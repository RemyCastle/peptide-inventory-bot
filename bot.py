#!/usr/bin/env python3
"""
Peptide Inventory Telegram Bot
──────────────────────────────
Multi-shop inventory, cart/checkout, payment options, admin confirmations.
Stock is deducted only after an admin confirms payment.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import backup as backup_mod
import db
import inventory_import
import payment_templates as pt
import reports
import setup_wizard
import token_pool
from config import (
    ACTIVE_BOT_INDEX,
    BACKUP_DIR,
    BACKUP_PASSPHRASE,
    BACKUP_RETENTION_DAYS,
    BRAND_NAME,
    CURRENCY_SYMBOL,
    DB_PATH,
    KIT_SIZE,
    LOG_PATH,
    OWNER_IDS,
    PUBLIC_BOT_USERNAME,
    RECOVERY_URL,
    TOKEN_FAILOVER,
    TOKEN_STATE_PATH,
    resolve_bot_tokens,
)

log = logging.getLogger("inventory_bot")


def setup_logging() -> None:
    """Log to stdout and a rotating file for post-crash debugging."""
    root = logging.getLogger()
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        log.warning("Could not open log file %s: %s", LOG_PATH, exc)


def brand_for(chat_id: int | None) -> str:
    if chat_id is None:
        return BRAND_NAME
    shop = db.get_shop(chat_id)
    return db.shop_display(shop)["brand_name"]


def symbol_for(chat_id: int | None) -> str:
    if chat_id is None:
        return CURRENCY_SYMBOL
    shop = db.get_shop(chat_id)
    return db.shop_display(shop)["currency_symbol"]

# ── Conversation states ──────────────────────────────────────────────────────
(
    ADD_PROD_NAME,
    ADD_PROD_PRICE,
    ADD_PROD_STOCK,
    ADD_PROD_DESC,
    ADD_PROD_UNIT,
    EDIT_PRICE_VALUE,
    EDIT_STOCK_VALUE,
    EDIT_NAME_VALUE,
    EDIT_UNIT_VALUE,
    ADD_PAY_NAME,
    ADD_PAY_INSTR,
    SHIP_FEE,
    SHIP_FREE,
    ADD_ADMIN_ID,
    CHECKOUT_NAME,
    CHECKOUT_ADDRESS,
    CHECKOUT_NOTES,
    CHECKOUT_VERIFY,
    PAYMENT_PROOF,
    TRACKING_INPUT,
    PAY_TPL_DETAILS,
    SEARCH_QUERY,
    EDIT_COA_VALUE,
    MASTER_FEE_INPUT,
    FRANCHISE_PROOF,
    EDIT_SHOP_TITLE,
    IMPORT_INVENTORY_FILE,
    MASS_EDIT_FILE,
    MIN_ORDER_QTY,
    EDIT_KIT_PRICE,
) = range(30)

# How long a pending admin/buyer prompt stays open (seconds)
AWAITING_TTL_SEC = 600


def force_reply(placeholder: str = "Type your answer...") -> ForceReply:
    """
    Open the user's keyboard and focus the input field.
    selective=True → only the prompted user in groups.
    Placeholder max length is 64 (Telegram limit).
    """
    ph = (placeholder or "Type your answer...").strip() or "Type your answer..."
    return ForceReply(selective=True, input_field_placeholder=ph[:64])

SYM = CURRENCY_SYMBOL


# ── Helpers ──────────────────────────────────────────────────────────────────


def money(n: float) -> str:
    return db.money(float(n), SYM)


def cart(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """product_id -> {"singles": int, "kits": int} (legacy int = singles)."""
    if "cart" not in context.user_data:
        context.user_data["cart"] = {}
    return context.user_data["cart"]


def cart_get_entry(context: ContextTypes.DEFAULT_TYPE, pid: int) -> dict[str, int]:
    return db.normalize_cart_entry(cart(context).get(int(pid)))


def cart_set_entry(
    context: ContextTypes.DEFAULT_TYPE, pid: int, singles: int, kits: int
) -> None:
    c = cart(context)
    s, k = max(0, int(singles)), max(0, int(kits))
    if s <= 0 and k <= 0:
        c.pop(int(pid), None)
    else:
        c[int(pid)] = {"singles": s, "kits": k}


def shop_id(context: ContextTypes.DEFAULT_TYPE, update: Update | None = None) -> int | None:
    """Active shop chat_id for this user session."""
    sid = context.user_data.get("shop_id")
    if sid is not None:
        return int(sid)
    if update and update.effective_chat:
        ch = update.effective_chat
        if ch.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            return int(ch.id)
    return None


def set_shop(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    context.user_data["shop_id"] = int(chat_id)


def set_awaiting(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    """Mark that the next text message answers a bot prompt."""
    context.user_data["awaiting"] = key
    context.user_data["awaiting_at"] = time.time()


def clear_awaiting(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting", None)
    context.user_data.pop("awaiting_at", None)


def awaiting_expired(context: ContextTypes.DEFAULT_TYPE) -> bool:
    at = context.user_data.get("awaiting_at")
    if at is None:
        return False
    if time.time() - float(at) > AWAITING_TTL_SEC:
        clear_awaiting(context)
        return True
    return False


async def accept_prompt_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    DMs: any plain text answers the open prompt (no Telegram 'Reply' required).
    Groups: must reply-to the bot's prompt so unrelated chat is ignored.
    """
    if awaiting_expired(context):
        if update.message:
            await update.message.reply_text(
                "That prompt expired. Tap the button again to restart."
            )
        return False

    chat = update.effective_chat
    msg = update.message
    if not chat or not msg:
        return False

    if chat.type == ChatType.PRIVATE:
        return True

    # Group / supergroup: require reply to a bot message
    reply = msg.reply_to_message
    bot_id = context.bot.id
    if (
        not reply
        or not reply.from_user
        or reply.from_user.id != bot_id
    ):
        await msg.reply_text(
            "In *groups*, please *reply* to my prompt message "
            "(long-press → Reply). That way I don't grab normal chat.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False
    return True


def user_label(user) -> str:
    name = " ".join(x for x in [user.first_name, user.last_name] if x) or "User"
    un = f" @{user.username}" if user.username else ""
    return f"{name}{un} (`{user.id}`)"


async def safe_edit(query, text: str, reply_markup=None) -> None:
    plain = (
        text.replace("*", "")
        .replace("_", "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
    )
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return
    except Exception as e:
        err = str(e).lower()
        # Fallback without markdown if parse fails
        if "parse" in err or "entities" in err or "can't parse" in err:
            try:
                await query.edit_message_text(plain, reply_markup=reply_markup)
                return
            except Exception as e2:
                log.warning("edit plain failed: %s", e2)
        elif "not modified" in err:
            return
        else:
            log.warning("edit failed: %s", e)
    # Last resort: send a new message so the user always gets feedback
    try:
        await query.message.reply_text(plain[:3500], reply_markup=reply_markup)
    except Exception as e3:
        log.warning("reply fallback failed: %s", e3)


def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🧬 Catalog", callback_data="cat"),
            InlineKeyboardButton("🔍 Search", callback_data="search"),
        ],
        [
            InlineKeyboardButton("🛒 Cart", callback_data="cart"),
            InlineKeyboardButton("📦 My Orders", callback_data="myorders"),
        ],
        [
            InlineKeyboardButton("ℹ️ Help", callback_data="help"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def back_main_kb(extra: list | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append([InlineKeyboardButton("« Main menu", callback_data="main")])
    return InlineKeyboardMarkup(rows)


# ── /start & shop selection ──────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    assert user and chat

    # Deep-link: /start shop_<chat_id> or setup_<chat_id>
    if context.args:
        arg0 = context.args[0]
        if arg0.startswith("shop_"):
            try:
                sid = int(arg0.removeprefix("shop_"))
                # Follow transfer aliases so old links still open the moved shop
                resolved = db.resolve_shop_chat_id(sid)
                shop = db.get_shop(resolved) or db.ensure_shop(resolved)
                set_shop(context, resolved)
                await _show_main(update, context, shop)
                return
            except ValueError:
                pass
        if arg0.startswith("collab_"):
            token = arg0.removeprefix("collab_")
            import collab

            collab.ensure_collab_tables()
            inv = collab.get_invite(token)
            if not inv:
                await update.message.reply_text("Collaboration invite not found or expired.")
                return
            if inv["status"] != "pending":
                await update.message.reply_text(f"Invite already {inv['status']}.")
                return
            host = db.get_shop(int(inv["host_chat_id"]))
            host_title = host["title"] if host else str(inv["host_chat_id"])
            # List shops this user admins as guest candidates
            admin_shops = db.shops_for_admin(user.id)
            if not admin_shops:
                await update.message.reply_text(
                    f"Invite from *{host_title}* to share inventory.\n"
                    "You need to be admin of a shop to accept. Open /setup in your group first.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            buttons = [
                [
                    InlineKeyboardButton(
                        f"Accept as {s['title']}",
                        callback_data=f"collab_accept:{token}:{s['chat_id']}",
                    )
                ]
                for s in admin_shops
                if int(s["chat_id"]) != int(inv["host_chat_id"])
            ]
            if not buttons:
                await update.message.reply_text("No eligible guest shop (cannot use host shop).")
                return
            await update.message.reply_text(
                f"🤝 *Inventory collaboration invite*\n"
                f"Host shop: *{host_title}*\n"
                f"Default markup for shared items: {inv['default_markup_pct']}%\n\n"
                f"Pick which of *your* shops will share stock with them:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return
        if arg0.startswith("setup_"):
            try:
                sid = int(arg0.removeprefix("setup_"))
                context.user_data["setup_chat_id"] = sid
                await update.message.reply_text(
                    "Continuing shop setup…\nSend /setup to open the wizard, "
                    "or tap the button below.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "✅ Set up shop",
                                    callback_data=f"wizoffer:yes:{sid}",
                                )
                            ]
                        ]
                    ),
                )
                return
            except ValueError:
                pass
        if arg0.startswith("clone_"):
            token = arg0.removeprefix("clone_")
            import franchise

            franchise.ensure_franchise_tables()
            inv = franchise.get_clone_token(token)
            if not inv or inv["status"] != "pending":
                await update.message.reply_text("Clone link not found or already used.")
                return
            context.user_data["pending_clone_token"] = token
            await update.message.reply_text(
                "📋 *Clone shop ready*\n\n"
                f"Source shop: `{inv['source_chat_id']}`\n\n"
                "1. Open the *new Telegram group* (bot must be a member)\n"
                f"2. Send this command *in that group*:\n"
                f"`/claim_clone {token}`\n\n"
                "That group gets separate prices + shared inventory with the master stock.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if arg0.startswith("transfer_"):
            token = arg0.removeprefix("transfer_")
            inv = db.get_transfer_token(token)
            if not inv or inv["status"] != "pending":
                await update.message.reply_text("Transfer link not found or already used.")
                return
            context.user_data["pending_transfer_token"] = token
            src = db.get_shop(int(inv["source_chat_id"]))
            src_title = (src or {}).get("title") or str(inv["source_chat_id"])
            await update.message.reply_text(
                "🚚 *Move shop to another group*\n\n"
                f"Shop: *{src_title}* (`{inv['source_chat_id']}`)\n\n"
                "This *moves* the whole shop (catalog, stock, orders, payments) — "
                "it does *not* clone it.\n\n"
                "1. Add this bot to the *destination* Telegram group\n"
                "2. In that group send:\n"
                f"`/claim_transfer {token}`\n\n"
                "Only a shop admin can claim. Destination group must be empty "
                "(no existing products/orders).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        shop = db.ensure_shop(chat.id, title=chat.title or "Shop")
        set_shop(context, chat.id)
        # Auto-promote group creator/admins? Only owners via OWNER_IDS + explicit add.
        text = (
            f"*{BRAND_NAME}*\n"
            f"Shop linked to this group: *{shop['title']}*\n\n"
            "Members: use /catalog or /order in DM with the bot for a private cart.\n"
            "Admins: /admin here or in DM after being added."
        )
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(db.is_admin(chat.id, user.id)),
        )
        return

    # Private chat
    shops = db.shops_for_admin(user.id)
    # Also allow picking any known shop if they have a deep link — otherwise list admin shops
    # or prompt to open from a group.
    all_shops = shops
    if not all_shops:
        # If owner, list all; else show setup help
        if db.is_owner(user.id):
            with db.get_db() as conn:
                rows = conn.execute("SELECT * FROM shops ORDER BY title").fetchall()
            all_shops = [dict(r) for r in rows]

    if len(all_shops) == 1:
        set_shop(context, all_shops[0]["chat_id"])
        await _show_main(update, context, all_shops[0])
        return

    if all_shops:
        buttons = [
            [InlineKeyboardButton(s["title"] or str(s["chat_id"]), callback_data=f"pickshop:{s['chat_id']}")]
            for s in all_shops
        ]
        await update.message.reply_text(
            f"*{BRAND_NAME}*\nSelect a shop:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # No shops yet — owner can bootstrap a personal shop (uses private chat id)
    if db.is_owner(user.id):
        shop = db.ensure_shop(user.id, title=f"{user.first_name or 'Owner'}'s Shop")
        db.add_admin(user.id, user.id, user.username, user.id)
        set_shop(context, user.id)
        await update.message.reply_text(
            f"*{BRAND_NAME}*\n"
            "No shops yet — created a personal shop for you (owner).\n"
            "Add products via Admin Panel. To use a group shop, add the bot to a group and /start there.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(True),
        )
        return

    await update.message.reply_text(
        f"*{BRAND_NAME}*\n\n"
        "No shop is linked yet.\n"
        "• Join a seller group that uses this bot, or\n"
        "• Open the bot from a group with `/start`, or\n"
        "• Use a shop link: `t.me/<bot>?start=shop_<id>`\n\n"
        "Owners: set your Telegram ID in `OWNER_IDS` and /start again.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_pickshop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid = int(query.data.split(":")[1])
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    set_shop(context, sid)
    await _show_main(update, context, shop, edit=True)


async def _show_main(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    shop: dict,
    edit: bool = False,
) -> None:
    user = update.effective_user
    assert user
    is_adm = db.is_admin(shop["chat_id"], user.id)
    welcome = shop.get("welcome_text") or (
        f"Welcome to *{shop['title']}*.\nBrowse the catalog, add items to your cart, and checkout."
    )
    ship = ""
    if shop.get("shipping_enabled"):
        fee = money(float(shop["shipping_fee"]))
        free = float(shop.get("free_shipping_above") or 0)
        ship = f"\n🚚 Shipping: {fee}"
        if free > 0:
            ship += f" (free over {money(free)})"
    display = db.shop_display(shop)
    min_line = ""
    if int(display["min_order_qty"]) > 0:
        min_line = f"\n📦 {db.format_min_order_rule(display['min_order_qty'], display['min_order_label'])}"
    text = f"*{BRAND_NAME}*\n🏪 *{shop['title']}*\n\n{welcome}{ship}{min_line}"
    kb = main_menu_kb(is_adm)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif update.callback_query:
        await safe_edit(update.callback_query, text, kb)


async def cb_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid = shop_id(context, update)
    if sid is None:
        await safe_edit(query, "No shop selected. Send /start")
        return ConversationHandler.END
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    await _show_main(update, context, shop, edit=True)
    return ConversationHandler.END


# ── Catalog & cart ───────────────────────────────────────────────────────────


def product_list_keyboard(
    products: list,
    *,
    footer_rows: list | None = None,
) -> InlineKeyboardMarkup:
    """Shared product-card buttons used by Catalog and Search."""
    buttons = []
    for p in products:
        stock = int(p["stock"])
        label = f"{p['name']} · {money(p['price'])}"
        if stock <= 0:
            label += " (out)"
        else:
            label += f" · {stock} left"
        row = [InlineKeyboardButton(label, callback_data=f"prod:{p['id']}")]
        if db.product_has_coa(p):
            row.append(
                InlineKeyboardButton("📄 COA", callback_data=f"viewcoa:{p['id']}")
            )
        buttons.append(row)
    if footer_rows:
        buttons.extend(footer_rows)
    else:
        buttons.append(
            [
                InlineKeyboardButton("🛒 Cart", callback_data="cart"),
                InlineKeyboardButton("« Menu", callback_data="main"),
            ]
        )
    return InlineKeyboardMarkup(buttons)


def product_detail_keyboard(p: dict, *, stock: int) -> InlineKeyboardMarkup:
    """Buyer product detail: add-to-cart + optional kit + COA file button."""
    buttons: list[list] = []
    if stock > 0:
        add_row = [
            InlineKeyboardButton("＋1", callback_data=f"add:{p['id']}:1"),
            InlineKeyboardButton("＋2", callback_data=f"add:{p['id']}:2"),
            InlineKeyboardButton("＋5", callback_data=f"add:{p['id']}:5"),
        ]
        buttons.append(add_row)
        # Kit option only when kit_price set AND stock can cover a full kit
        if db.kit_option_available(p, stock=stock):
            kp = db.product_kit_price(p)
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"📦 Kit of {KIT_SIZE} · {money(kp)}",
                        callback_data=f"addkit:{p['id']}",
                    )
                ]
            )
    if db.product_has_coa(p):
        buttons.append(
            [InlineKeyboardButton("📄 COA", callback_data=f"viewcoa:{p['id']}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("« Catalog", callback_data="cat"),
            InlineKeyboardButton("🛒 Cart", callback_data="cart"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def _reply_or_edit(
    update: Update,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
    *,
    edit: bool = False,
) -> None:
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    elif update.message:
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
    elif update.callback_query:
        await safe_edit(update.callback_query, text, kb)


async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_search", None)
    await _send_catalog(update, context)


async def cb_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting_search", None)
    await _send_catalog(update, context, edit=True)


async def _send_catalog(
    update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False
) -> None:
    sid = shop_id(context, update)
    if sid is None:
        await _reply_or_edit(update, "No shop selected. Send /start first.", edit=edit)
        return

    shop = db.get_shop(sid) or db.ensure_shop(sid)
    try:
        import collab

        collab.ensure_collab_tables()
        products = collab.catalog_for_host(sid)
    except Exception:
        products = db.list_products(sid, active_only=True)
        for p in products:
            p["sell_price"] = float(p["price"])
            p["is_guest"] = False

    if not products:
        text = f"*{shop['title']}* — catalog is empty."
        await _reply_or_edit(update, text, back_main_kb(), edit=edit)
        return

    # Present guest shares with sell price in list labels via name suffix
    display_products = []
    for p in products:
        q = dict(p)
        sell = float(p.get("sell_price", p.get("price", 0)))
        if p.get("is_guest"):
            guest = p.get("guest_title") or "partner"
            q["name"] = f"{p['name']} · {money(sell)} (+{guest})"
            q["price"] = sell
        display_products.append(q)

    text = (
        f"🧬 *{shop['title']} — Catalog*\n"
        f"_Includes partner stock when shared._\n\n"
        f"Tap a product to add to cart:"
    )
    footer = [
        [
            InlineKeyboardButton("🔍 Search", callback_data="search"),
            InlineKeyboardButton("🛒 Cart", callback_data="cart"),
        ],
        [InlineKeyboardButton("« Menu", callback_data="main")],
    ]
    kb = product_list_keyboard(display_products, footer_rows=footer)
    await _reply_or_edit(update, text, kb, edit=edit)


async def _send_search_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query_text: str,
    *,
    edit: bool = False,
) -> None:
    sid = shop_id(context, update)
    if sid is None:
        await _reply_or_edit(update, "No shop selected. Send /start first.", edit=edit)
        return

    q = (query_text or "").strip()
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    if not q:
        await _reply_or_edit(
            update,
            "Type what you're looking for, or use `/search tren`.",
            back_main_kb(
                [
                    [
                        InlineKeyboardButton("🧬 Catalog", callback_data="cat"),
                        InlineKeyboardButton("🔍 Search", callback_data="search"),
                    ]
                ]
            ),
            edit=edit,
        )
        return

    products = db.search_products(sid, q, active_only=True, limit=20)
    if not products:
        text = (
            f"No products matched `{q}`.\n"
            "Try browsing the full catalog:"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🧬 Browse catalog", callback_data="cat")],
                [InlineKeyboardButton("🔍 Search again", callback_data="search")],
                [InlineKeyboardButton("« Menu", callback_data="main")],
            ]
        )
        await _reply_or_edit(update, text, kb, edit=edit)
        return

    text = (
        f"🔍 *{shop['title']} — Search*\n"
        f"Results for `{q}` ({len(products)}):\n\n"
        "Tap a product to view / add to cart:"
    )
    footer = [
        [
            InlineKeyboardButton("🧬 Catalog", callback_data="cat"),
            InlineKeyboardButton("🔍 New search", callback_data="search"),
        ],
        [
            InlineKeyboardButton("🛒 Cart", callback_data="cart"),
            InlineKeyboardButton("« Menu", callback_data="main"),
        ],
    ]
    kb = product_list_keyboard(products, footer_rows=footer)
    await _reply_or_edit(update, text, kb, edit=edit)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ /search <term> — per-shop product search. """
    context.user_data.pop("awaiting_search", None)
    if context.args:
        clear_awaiting(context)
        await _send_search_results(update, context, " ".join(context.args))
        return ConversationHandler.END
    if shop_id(context, update) is None:
        await update.message.reply_text("No shop selected. Send /start first.")
        return ConversationHandler.END
    set_awaiting(context, "search")
    await update.message.reply_text(
        "🔍 *Search*\nType what you're looking for:\n(/cancel to abort)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Search products..."),
    )
    return SEARCH_QUERY


async def cb_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid = shop_id(context, update)
    if sid is None:
        await safe_edit(query, "No shop selected. Send /start first.")
        return ConversationHandler.END
    set_awaiting(context, "search")
    # New message with ForceReply (edit can't always focus keyboard reliably)
    await query.message.reply_text(
        "🔍 *Search*\nType what you're looking for:\n(/cancel to abort)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Search products..."),
    )
    return SEARCH_QUERY


async def on_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture free-text query after Search button / bare /search."""
    if not await accept_prompt_message(update, context):
        return SEARCH_QUERY
    text = (update.message.text or "").strip()
    clear_awaiting(context)
    await _send_search_results(update, context, text)
    return ConversationHandler.END


def _product_with_stock(pid: int) -> dict | None:
    """Product dict with clone-aware effective stock for buyer cart checks."""
    p = db.get_product(pid)
    if not p:
        return None
    try:
        from franchise import enrich_product_stock, ensure_franchise_tables

        ensure_franchise_tables()
        return enrich_product_stock(p)
    except Exception:
        return dict(p)


def _catalog_entry_for_shop(sid: int | None, pid: int) -> dict | None:
    """
    Product only if it belongs in this shop's buyer catalog:
    own inventory, or an active collab share into this host.
    Prevents cross-shop product id leakage via crafted callbacks.
    """
    if sid is None:
        return None
    try:
        import collab

        collab.ensure_collab_tables()
        for item in collab.catalog_for_host(int(sid)):
            if int(item["id"]) == int(pid):
                # Overlay clone-aware stock on the catalog row
                live = _product_with_stock(int(pid))
                if not live or not live.get("active"):
                    return None
                out = dict(item)
                out["stock"] = int(live.get("stock") or 0)
                out["active"] = live.get("active", 1)
                out["kit_price"] = live.get("kit_price")
                out["description"] = live.get("description") or item.get("description") or ""
                out["unit"] = live.get("unit") or item.get("unit") or "vial"
                # Buyer-facing price = sell price (includes collab markup)
                out["price"] = float(item.get("sell_price", live.get("price") or 0))
                return out
    except Exception:
        p = _product_with_stock(int(pid))
        if p and p.get("active") and int(p.get("chat_id") or 0) == int(sid):
            return dict(p)
    return None


async def cb_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pid = int(query.data.split(":")[1])
    sid = shop_id(context, update)
    p = _catalog_entry_for_shop(sid, pid)
    if not p or not p["active"]:
        await query.answer("Product unavailable in this shop", show_alert=True)
        return
    stock = int(p["stock"])
    text = (
        f"*{p['name']}*\n"
        f"Price: *{money(p['price'])}* / {p.get('unit') or 'vial'}\n"
        f"Stock: {stock}\n"
    )
    if p.get("is_guest"):
        guest = p.get("guest_title") or "partner"
        text += f"_Partner stock ({guest})_\n"
    if db.kit_option_available(p, stock=stock):
        kp = db.product_kit_price(p)
        text += f"Kit of {KIT_SIZE}: *{money(kp)}*\n"
    elif db.product_kit_price(p) is not None and stock < KIT_SIZE:
        text += f"_Kit of {KIT_SIZE} unavailable (need {KIT_SIZE}+ in stock)_\n"
    if p.get("description"):
        text += f"\n{p['description']}\n"
    if stock <= 0:
        text += "\n_Currently out of stock._"
    if db.product_has_coa(p):
        text += "\n_COA available — tap 📄 COA for the file and/or link._"
    await safe_edit(query, text, product_detail_keyboard(p, stock=stock))


async def cb_add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, pid_s, qty_s = query.data.split(":")
    pid, qty = int(pid_s), int(qty_s)
    sid = shop_id(context, update)
    p = _catalog_entry_for_shop(sid, pid)
    if not p or not p["active"]:
        await query.answer("Unavailable in this shop", show_alert=True)
        return
    stock = int(p["stock"])
    e = cart_get_entry(context, pid)
    new_vials = db.cart_entry_vials(e) + qty
    if new_vials > stock:
        await query.answer(f"Only {stock} available", show_alert=True)
        return
    cart_set_entry(context, pid, e["singles"] + qty, e["kits"])
    await query.answer(
        f"Added {qty}× {p['name']} (cart: {new_vials} vials)"
    )


async def cb_add_kit_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add one kit (KIT_SIZE vials) at kit_price when stock allows."""
    query = update.callback_query
    pid = int(query.data.split(":")[1])
    sid = shop_id(context, update)
    p = _catalog_entry_for_shop(sid, pid)
    if not p or not p["active"]:
        await query.answer("Unavailable in this shop", show_alert=True)
        return
    stock = int(p["stock"])
    if not db.kit_option_available(p, stock=stock):
        await query.answer(
            f"Kit unavailable (need {KIT_SIZE}+ in stock)",
            show_alert=True,
        )
        return
    e = cart_get_entry(context, pid)
    new_vials = db.cart_entry_vials(e) + int(KIT_SIZE)
    if new_vials > stock:
        await query.answer(f"Only {stock} available", show_alert=True)
        return
    cart_set_entry(context, pid, e["singles"], e["kits"] + 1)
    kp = db.product_kit_price(p)
    await query.answer(
        f"Added kit of {KIT_SIZE} · {money(kp)} (cart: {new_vials} vials)"
    )


async def cb_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _render_cart(update, context, edit=True)


async def cmd_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render_cart(update, context, edit=False)


async def _render_cart(
    update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool
) -> None:
    sid = shop_id(context, update)
    c = cart(context)
    if not c:
        text = "🛒 Your cart is empty."
        kb = back_main_kb([[InlineKeyboardButton("🧬 Catalog", callback_data="cat")]])
        if edit and update.callback_query:
            await safe_edit(update.callback_query, text, kb)
        elif update.message:
            await update.message.reply_text(text, reply_markup=kb)
        return

    shop = db.get_shop(sid) if sid else None
    # Refresh products and drop items not in this shop's catalog (isolation)
    prod_map: dict[int, dict] = {}
    for pid in list(c.keys()):
        p = _catalog_entry_for_shop(sid, int(pid))
        if p:
            prod_map[int(pid)] = p
        else:
            c.pop(pid, None)
    kit_notes = db.sanitize_cart_kits(c, prod_map)

    lines = ["🛒 *Your cart*\n"]
    subtotal = 0.0
    buttons = []
    for pid, entry in list(c.items()):
        p = prod_map.get(int(pid))
        if not p or not p["active"]:
            c.pop(pid, None)
            continue
        e = db.normalize_cart_entry(entry)
        line = db.cart_entry_line_total(p, e)
        subtotal += line
        bits = []
        if e["kits"] > 0:
            kp = db.product_kit_price(p) or 0
            bits.append(f"{e['kits']} kit(s) @ {money(kp)}")
        if e["singles"] > 0:
            bits.append(f"{e['singles']} vial(s) @ {money(p['price'])}")
        bits_s = " + ".join(bits) if bits else f"{db.cart_entry_vials(e)} vials"
        lines.append(f"• {p['name']}: {bits_s} = {money(line)}")
        buttons.append(
            [
                InlineKeyboardButton(f"− {p['name'][:18]}", callback_data=f"sub:{pid}"),
                InlineKeyboardButton("🗑", callback_data=f"rm:{pid}"),
            ]
        )
    for note in kit_notes:
        lines.append(f"_{note}_")

    if not c:
        text = "🛒 Your cart is empty."
        kb = back_main_kb([[InlineKeyboardButton("🧬 Catalog", callback_data="cat")]])
        if edit and update.callback_query:
            await safe_edit(update.callback_query, text, kb)
        return

    shipping = db.calc_shipping(shop, subtotal) if shop else 0.0
    total = subtotal + shipping
    qty_total = db.cart_quantity_total(c)
    lines += [
        "",
        f"Items: {qty_total}",
        f"Subtotal: {money(subtotal)}",
        f"Shipping: {money(shipping)}",
        f"*Total: {money(total)}*",
    ]
    if shop and shop.get("shipping_enabled") and float(shop.get("free_shipping_above") or 0) > 0:
        free_at = float(shop["free_shipping_above"])
        if subtotal < free_at:
            lines.append(f"_Free shipping over {money(free_at)}_")
    min_ok = True
    if shop:
        min_ok, min_msg = db.check_min_order(shop, qty_total)
        if not min_ok:
            # Strip markdown stars for cart note lines
            rule = db.format_min_order_rule(
                int(db.shop_display(shop)["min_order_qty"]),
                str(db.shop_display(shop)["min_order_label"]),
            )
            lines.append(f"⚠️ _{rule} — add more to checkout_")

    if min_ok:
        buttons.append([InlineKeyboardButton("✅ Checkout", callback_data="checkout")])
    else:
        buttons.append(
            [InlineKeyboardButton("⚠️ Min order not met", callback_data="checkout")]
        )
    buttons.append(
        [
            InlineKeyboardButton("🧬 Catalog", callback_data="cat"),
            InlineKeyboardButton("🗑 Clear", callback_data="clearcart"),
            InlineKeyboardButton("« Menu", callback_data="main"),
        ]
    )
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(buttons)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cb_sub_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    pid = int(query.data.split(":")[1])
    e = cart_get_entry(context, pid)
    if e["singles"] > 0:
        e["singles"] -= 1
    elif e["kits"] > 0:
        # Break one kit into (KIT_SIZE - 1) singles
        e["kits"] -= 1
        e["singles"] += int(KIT_SIZE) - 1
    cart_set_entry(context, pid, e["singles"], e["kits"])
    await query.answer("Updated")
    await _render_cart(update, context, edit=True)


async def cb_rm_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    pid = int(query.data.split(":")[1])
    cart(context).pop(pid, None)
    await query.answer("Removed")
    await _render_cart(update, context, edit=True)


async def cb_clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data["cart"] = {}
    await query.answer("Cart cleared")
    await _render_cart(update, context, edit=True)


# ── Checkout conversation ────────────────────────────────────────────────────


async def cb_checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid = shop_id(context, update)
    c = cart(context)
    if not sid or not c:
        await safe_edit(query, "Cart is empty or no shop selected.")
        return ConversationHandler.END

    shop = db.get_shop(sid) or db.ensure_shop(sid)

    # Drop kit deals if stock fell below KIT_SIZE
    prod_map = {}
    for pid in list(c.keys()):
        p = _product_with_stock(int(pid))
        if p:
            prod_map[int(pid)] = p
    db.sanitize_cart_kits(c, prod_map)
    if not c:
        await safe_edit(
            query,
            "Cart is empty or items became unavailable.",
            back_main_kb([[InlineKeyboardButton("🧬 Catalog", callback_data="cat")]]),
        )
        return ConversationHandler.END

    ok_min, min_msg = db.check_min_order(shop, db.cart_quantity_total(c))
    if not ok_min:
        await safe_edit(
            query,
            min_msg,
            back_main_kb(
                [
                    [InlineKeyboardButton("🧬 Catalog", callback_data="cat")],
                    [InlineKeyboardButton("🛒 Cart", callback_data="cart")],
                ]
            ),
        )
        return ConversationHandler.END

    methods = db.list_payment_methods(sid, active_only=True)
    if not methods:
        log.warning("Checkout blocked: shop %s has no payment methods", sid)
        await safe_edit(
            query,
            "⚠️ *Checkout unavailable*\n\n"
            "This shop has *no payment methods* set up yet.\n"
            "Shop owner: open *Admin → 💳 Payments* and add Cash App / Venmo / etc., "
            "then the buyer can checkout.",
            back_main_kb(),
        )
        return ConversationHandler.END

    # Build cart items & validate stock (host + shared guest inventory)
    import collab

    collab.ensure_collab_tables()
    catalog = {int(x["id"]): x for x in collab.catalog_for_host(sid)}
    items = []
    for pid, raw_entry in list(c.items()):
        e = db.normalize_cart_entry(raw_entry)
        entry = catalog.get(int(pid))
        p = _product_with_stock(int(pid))
        need = db.cart_entry_vials(e)
        if not p or not p["active"] or int(p["stock"]) < need:
            await safe_edit(
                query,
                f"Stock issue with product id {pid}. Adjust cart and try again.",
                back_main_kb([[InlineKeyboardButton("🛒 Cart", callback_data="cart")]]),
            )
            return ConversationHandler.END
        if entry is None and int(p["chat_id"]) != int(sid):
            await safe_edit(
                query,
                f"{p['name']} is no longer shared to this shop. Remove it from cart.",
                back_main_kb([[InlineKeyboardButton("🛒 Cart", callback_data="cart")]]),
            )
            return ConversationHandler.END
        sell = float(entry["sell_price"]) if entry else float(p["price"])
        owner = int(entry["owner_chat_id"]) if entry else int(p["chat_id"])
        if e["kits"] > 0:
            if not db.kit_option_available(p, stock=int(p["stock"])):
                await safe_edit(
                    query,
                    f"Kit pricing for {p['name']} is no longer available "
                    f"(need {KIT_SIZE}+ in stock). Adjust cart.",
                    back_main_kb(
                        [[InlineKeyboardButton("🛒 Cart", callback_data="cart")]]
                    ),
                )
                return ConversationHandler.END
            items.append(
                {
                    "product_id": pid,
                    "product_name": f"{p['name']} (kit of {KIT_SIZE})",
                    "unit_price": sell,  # overwritten at create for host; collab uses is_kit
                    "quantity": e["kits"] * int(KIT_SIZE),
                    "owner_chat_id": owner,
                    "is_kit": True,
                }
            )
        if e["singles"] > 0:
            items.append(
                {
                    "product_id": pid,
                    "product_name": p["name"],
                    "unit_price": sell,
                    "quantity": e["singles"],
                    "owner_chat_id": owner,
                    "is_kit": False,
                }
            )
    if not items:
        await safe_edit(
            query,
            "Cart is empty. Adjust cart and try again.",
            back_main_kb([[InlineKeyboardButton("🛒 Cart", callback_data="cart")]]),
        )
        return ConversationHandler.END
    context.user_data["checkout_items"] = items
    context.user_data["checkout_multi"] = any(
        int(it["owner_chat_id"]) != int(sid) for it in items
    )

    buttons = [
        [InlineKeyboardButton(m["name"], callback_data=f"paym:{m['id']}")]
        for m in methods
    ]
    buttons.append([InlineKeyboardButton("« Cancel", callback_data="cart")])
    await safe_edit(
        query,
        "💳 *Select payment method:*\n(You'll get pay instructions after confirming shipping.)",
        InlineKeyboardMarkup(buttons),
    )
    return CHECKOUT_NAME  # intermediate; actual name asked after pay method


async def cb_pay_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mid = int(query.data.split(":")[1])
    method = db.get_payment_method(mid)
    if not method or not method["active"]:
        await safe_edit(query, "Payment method unavailable.")
        return ConversationHandler.END
    context.user_data["checkout_pay"] = method
    await query.message.reply_text(
        "📬 *Shipping — full name*\n\nSend the recipient's full name.\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Full name..."),
    )
    return CHECKOUT_NAME


async def checkout_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get("checkout_pay"):
        await update.message.reply_text(
            "Please pick a payment method from the buttons above first (or /cancel)."
        )
        return CHECKOUT_NAME
    if not await accept_prompt_message(update, context):
        return CHECKOUT_NAME
    context.user_data["ship_name"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "📬 *Shipping address*\n\n"
        "Send full address (street, city, state/region, ZIP, country).\n"
        "/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Shipping address..."),
    )
    return CHECKOUT_ADDRESS


async def checkout_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return CHECKOUT_ADDRESS
    context.user_data["ship_address"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "📝 Any delivery notes? (or send `-` for none)\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Delivery notes or - ..."),
    )
    return CHECKOUT_NOTES


async def checkout_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store notes then require explicit address verification before order create."""
    if not await accept_prompt_message(update, context):
        return CHECKOUT_NOTES
    notes = (update.message.text or "").strip()
    if notes == "-":
        notes = ""
    context.user_data["ship_notes"] = notes
    return await _prompt_address_verify(update, context)


async def _prompt_address_verify(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    ship_name = context.user_data.get("ship_name") or "—"
    ship_address = context.user_data.get("ship_address") or "—"
    notes = context.user_data.get("ship_notes") or "—"
    text = (
        "📬 *Please verify your shipping details*\n\n"
        f"*Name:* {ship_name}\n"
        f"*Address:*\n{ship_address}\n"
        f"*Notes:* {notes}\n\n"
        "Is this correct? You must confirm before we place the order."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes, looks correct", callback_data="shipok")],
            [InlineKeyboardButton("✏️ Edit name/address", callback_data="shipedit")],
            [InlineKeyboardButton("« Cancel checkout", callback_data="cart")],
        ]
    )
    if update.message:
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
    elif update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    return CHECKOUT_VERIFY


async def cb_ship_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📬 *Shipping — full name*\n\nSend the recipient's full name.\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Full name..."),
    )
    return CHECKOUT_NAME


async def cb_ship_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Address confirmed — create order with payment code."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    sid = shop_id(context, update)
    items = context.user_data.get("checkout_items") or []
    pay = context.user_data.get("checkout_pay")
    ship_name = context.user_data.get("ship_name") or ""
    ship_address = context.user_data.get("ship_address") or ""
    notes = context.user_data.get("ship_notes") or ""

    if not sid or not items or not pay or not user:
        await safe_edit(query, "Checkout expired. Start again from cart.", back_main_kb())
        return ConversationHandler.END
    if not ship_name.strip() or not ship_address.strip():
        await safe_edit(
            query,
            "Name and address are required. Tap Edit to fix.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("✏️ Edit", callback_data="shipedit")]]
            ),
        )
        return CHECKOUT_VERIFY

    import collab

    collab.ensure_collab_tables()
    if context.user_data.get("checkout_multi") or any(
        int(it.get("owner_chat_id") or sid) != int(sid) for it in items
    ):
        order = collab.create_order_multi(
            host_chat_id=sid,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            items=items,
            payment_method=pay,
            ship_name=ship_name,
            ship_address=ship_address,
            ship_notes=notes,
        )
    else:
        order = db.create_order(
            chat_id=sid,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            items=items,
            payment_method=pay,
            ship_name=ship_name,
            ship_address=ship_address,
            ship_notes=notes,
        )
    if not order:
        # Log why so dual-instance / min-order / stock failures are visible in bot.log
        try:
            shop_now = db.get_shop(sid) or {}
            qty = db.cart_quantity_total(items)
            ok_min, min_msg = db.check_min_order(shop_now, qty)
            log.warning(
                "Order create failed shop=%s user=%s items=%s qty=%s min_ok=%s multi=%s",
                sid,
                user.id,
                [(it.get("product_id"), it.get("quantity"), it.get("is_kit")) for it in items],
                qty,
                ok_min,
                bool(context.user_data.get("checkout_multi")),
            )
            if not ok_min:
                await safe_edit(
                    query,
                    min_msg or "Minimum order not met.",
                    back_main_kb(
                        [[InlineKeyboardButton("🛒 Cart", callback_data="cart")]]
                    ),
                )
                return ConversationHandler.END
        except Exception:
            log.exception("Order create failed (extra diagnostics error)")
        await safe_edit(
            query,
            "❌ Could not create order (stock may have changed, or kit pricing "
            "became unavailable). Check cart and try again.",
            back_main_kb([[InlineKeyboardButton("🛒 Cart", callback_data="cart")]]),
        )
        return ConversationHandler.END

    # Clear cart / checkout session
    context.user_data["cart"] = {}
    for k in (
        "checkout_items",
        "checkout_pay",
        "ship_name",
        "ship_address",
        "ship_notes",
    ):
        context.user_data.pop(k, None)

    items_db = db.get_order_items(order["id"])
    summary = db.format_order_summary(order, items_db, SYM)
    pay_instr = pay.get("instructions") or "(no instructions set)"
    code = (order.get("payment_code") or "").strip()

    text = (
        f"✅ *Order #{order['id']} placed*\n"
        f"Total: *{money(order['total'])}*\n\n"
        f"{summary}\n\n"
        f"💳 *Pay via {pay.get('name') or 'selected method'}*\n"
        f"{pay_instr}\n\n"
        f"🔑 *Payment code (put this in the memo/notes):*\n`{code}`\n\n"
        "⚠️ Use this code in Cash App / Zelle / Venmo notes so we can match your payment.\n\n"
        "After you pay, tap *I've Paid* and upload a screenshot if you can.\n"
        "_Inventory is only reduced after the seller confirms payment._"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid:{order['id']}")],
            [InlineKeyboardButton("❌ Cancel order", callback_data=f"cancelord:{order['id']}")],
            [InlineKeyboardButton("« Menu", callback_data="main")],
        ]
    )
    await safe_edit(query, text, kb)
    await _notify_admins_new_order(context, order, items_db)
    return ConversationHandler.END


def _shop_admin_recipients(
    chat_id: int,
    *,
    local_only: bool = False,
) -> set[int]:
    """
    Recipients for order/sale alerts.
    local_only=True → only this shop's admins table (no global OWNER_IDS fan-out).
    Used for franchisee shops so the main shop is not notified early.
    """
    recipients: set[int] = set()
    for a in db.list_admins(int(chat_id)):
        recipients.add(int(a["user_id"]))
    if not local_only:
        recipients |= set(OWNER_IDS)
    # If somehow no local admins, owners still get primary-shop alerts
    if not recipients and not local_only:
        recipients |= set(OWNER_IDS)
    return recipients


def _master_shop_recipients(franchisee_chat_id: int) -> set[int]:
    """Main shop admins + owners — for remittance proof after franchisee confirms sale."""
    import franchise

    franchise.ensure_franchise_tables()
    recipients: set[int] = set(OWNER_IDS)
    mid = franchise.master_chat_id_for(int(franchisee_chat_id))
    if mid is not None:
        for a in db.list_admins(int(mid)):
            recipients.add(int(a["user_id"]))
    return recipients


def _is_franchisee_order(order: dict) -> bool:
    try:
        import franchise

        franchise.ensure_franchise_tables()
        return franchise.is_franchisee_shop(int(order["chat_id"]))
    except Exception:
        return False


async def _notify_admins_sale_report(
    context,
    order: dict,
    items: list[dict],
    *,
    headline: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    audience: str = "auto",
) -> None:
    """
    Push full sale details.
    audience:
      auto   — franchisee shop → local franchisee admins only;
               primary shop → local admins + owners
      local  — shop admins only
      master — main shop (owners + master admins) for franchisee remittance
      all    — local + owners
    """
    text = db.format_sale_admin_report(
        order, items, SYM, headline=headline
    )
    if audience == "master":
        recipients = _master_shop_recipients(int(order["chat_id"]))
    elif audience == "local":
        recipients = _shop_admin_recipients(int(order["chat_id"]), local_only=True)
    elif audience == "all":
        recipients = _shop_admin_recipients(int(order["chat_id"]), local_only=False)
    else:
        # auto
        if _is_franchisee_order(order):
            recipients = _shop_admin_recipients(int(order["chat_id"]), local_only=True)
        else:
            recipients = _shop_admin_recipients(int(order["chat_id"]), local_only=False)

    for uid in recipients:
        try:
            await context.bot.send_message(
                uid,
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.info("Could not notify admin %s of sale: %s", uid, e)


async def _notify_admins_new_order(context, order: dict, items: list[dict]) -> None:
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm + tracking", callback_data=f"admconfirm:{order['id']}"
                ),
                InlineKeyboardButton("❌ Reject", callback_data=f"admreject:{order['id']}"),
            ],
            [InlineKeyboardButton("📋 Orders", callback_data="adm_orders")],
        ]
    )
    note = ""
    if _is_franchisee_order(order):
        note = " (franchisee — main shop not notified yet)"
    await _notify_admins_sale_report(
        context,
        order,
        items,
        headline=f"NEW ORDER (payment pending){note}",
        reply_markup=kb,
        audience="auto",
    )


async def _notify_admins_payment_claim(
    context, order: dict, items: list[dict]
) -> None:
    oid = order["id"]
    code = (order.get("payment_code") or "—").strip()
    proof = "screenshot attached" if order.get("payment_proof_file_id") else "no screenshot"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm + tracking", callback_data=f"admconfirm:{oid}"
                ),
                InlineKeyboardButton("❌ Reject", callback_data=f"admreject:{oid}"),
            ],
            [InlineKeyboardButton(f"View #{oid}", callback_data=f"vieword:{oid}")],
        ]
    )
    note = ""
    if _is_franchisee_order(order):
        note = " — main shop not notified yet"
    headline = f"PAYMENT CLAIM — buyer says paid ({proof}){note}"
    await _notify_admins_sale_report(
        context,
        order,
        items,
        headline=headline,
        reply_markup=kb,
        audience="auto",
    )
    # Customer payment proof → franchisee admins only (not main) until remittance step
    for uid in _shop_admin_recipients(
        order["chat_id"], local_only=_is_franchisee_order(order)
    ):
        try:
            fid = (order.get("payment_proof_file_id") or "").strip()
            if fid:
                ftype = (order.get("payment_proof_file_type") or "photo").lower()
                try:
                    if ftype == "document":
                        await context.bot.send_document(
                            uid,
                            document=fid,
                            caption=f"Customer payment proof — order #{oid} code {code}",
                        )
                    else:
                        await context.bot.send_photo(
                            uid,
                            photo=fid,
                            caption=f"Customer payment proof — order #{oid} code {code}",
                        )
                except Exception as e:
                    log.info("Could not send proof to admin %s: %s", uid, e)
        except Exception:
            pass


async def _forward_franchise_sale_to_master(
    context,
    order: dict,
    items: list[dict],
) -> None:
    """Notify main shop only after franchisee confirmed pay + submitted remittance proof."""
    import franchise

    franchise.ensure_franchise_tables()
    order = db.get_order(int(order["id"])) or order
    if not franchise.is_franchisee_shop(int(order["chat_id"])):
        return
    if order.get("status") != "paid":
        return
    if not franchise.franchise_forwarded(order):
        return
    mid = franchise.master_chat_id_for(int(order["chat_id"]))
    headline = (
        f"FRANCHISEE REMITTANCE — sale forwarded to main shop "
        f"(franchisee shop {order['chat_id']}"
        + (f" / master {mid}" if mid else "")
        + ")"
    )
    await _notify_admins_sale_report(
        context,
        order,
        items,
        headline=headline,
        audience="master",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"View #{order['id']}", callback_data=f"vieword:{order['id']}")]]
        ),
    )
    # Remittance proof media to main
    fid = (order.get("franchise_master_proof_file_id") or "").strip()
    if not fid:
        return
    ftype = (order.get("franchise_master_proof_file_type") or "photo").lower()
    caption = (
        f"Franchisee proof of payment to main — order #{order['id']} "
        f"code {order.get('payment_code') or '—'}"
    )
    for uid in _master_shop_recipients(int(order["chat_id"])):
        try:
            if ftype == "document":
                await context.bot.send_document(uid, document=fid, caption=caption)
            else:
                await context.bot.send_photo(uid, photo=fid, caption=caption)
        except Exception as e:
            log.info("Could not send franchise proof to master admin %s: %s", uid, e)


async def cb_franchise_proof_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Franchisee admin: send remittance proof so main shop gets the sale."""
    query = update.callback_query
    user = update.effective_user
    oid = int(query.data.split(":")[1])
    order = db.get_order(oid)
    if not order or not user or not db.is_admin(order["chat_id"], user.id):
        await query.answer("Not allowed", show_alert=True)
        return ConversationHandler.END
    if not _is_franchisee_order(order):
        await query.answer("Not a franchisee order", show_alert=True)
        return ConversationHandler.END
    if order["status"] != "paid":
        await query.answer("Confirm customer payment first", show_alert=True)
        return ConversationHandler.END
    import franchise

    if franchise.franchise_forwarded(order):
        await query.answer("Already sent to main shop", show_alert=True)
        return ConversationHandler.END
    context.user_data["franchise_proof_order_id"] = oid
    set_awaiting(context, "franchise_proof")
    await query.answer()
    await query.message.reply_text(
        f"Send *proof of payment to the main shop* for order #{oid}.\n"
        "Photo or PDF of your remittance / transfer to main.\n"
        "Main shop is *not* notified until you send this.\n"
        "/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Proof photo or PDF..."),
    )
    return FRANCHISE_PROOF


async def franchise_proof_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await accept_prompt_message(update, context):
        return FRANCHISE_PROOF
    user = update.effective_user
    oid = context.user_data.get("franchise_proof_order_id")
    if not oid or not user:
        clear_awaiting(context)
        return ConversationHandler.END
    order = db.get_order(int(oid))
    if not order or not db.is_admin(order["chat_id"], user.id):
        await update.message.reply_text("Not allowed.")
        clear_awaiting(context)
        return ConversationHandler.END

    file_id = None
    file_type = "photo"
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"
    else:
        await update.message.reply_text(
            "Send a *photo* or *PDF document* as proof, or /cancel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return FRANCHISE_PROOF

    import franchise

    ok, msg = franchise.set_franchise_master_proof(int(oid), file_id, file_type)
    clear_awaiting(context)
    context.user_data.pop("franchise_proof_order_id", None)
    if not ok:
        await update.message.reply_text(f"Could not save proof: {msg}")
        return ConversationHandler.END

    order = db.get_order(int(oid))
    items = db.get_order_items(int(oid))
    await update.message.reply_text(
        f"✅ {msg}\nMain shop has been notified of order #{oid}."
    )
    await _forward_franchise_sale_to_master(context, order, items)
    return ConversationHandler.END


async def cb_customer_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start payment-proof step after customer claims payment."""
    query = update.callback_query
    oid = int(query.data.split(":")[1])
    order = db.get_order(oid)
    user = update.effective_user
    if not order or order["user_id"] != user.id:
        await query.answer("Not your order", show_alert=True)
        return ConversationHandler.END
    if order["status"] not in ("pending_payment", "awaiting_confirmation"):
        await query.answer(f"Status: {order['status']}", show_alert=True)
        return ConversationHandler.END
    context.user_data["proof_order_id"] = oid
    set_awaiting(context, "payment_proof")
    code = (order.get("payment_code") or "—").strip()
    await query.answer()
    await query.message.reply_text(
        f"💵 *Payment proof for order #{oid}*\n"
        f"Payment code was: `{code}`\n\n"
        "Please *send a screenshot* of the payment (photo).\n"
        "Or tap *Skip* if you can't attach one right now.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⏭ Skip screenshot", callback_data=f"skipproof:{oid}")],
                [InlineKeyboardButton("« Cancel", callback_data="main")],
            ]
        ),
    )
    return PAYMENT_PROOF


async def payment_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return PAYMENT_PROOF
    oid = context.user_data.get("proof_order_id")
    user = update.effective_user
    msg = update.message
    if not oid or not user or not msg:
        clear_awaiting(context)
        return ConversationHandler.END
    order = db.get_order(int(oid))
    if not order or order["user_id"] != user.id:
        await msg.reply_text("Order not found.")
        clear_awaiting(context)
        return ConversationHandler.END

    file_id = None
    file_type = "photo"
    if msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = "photo"
    elif msg.document:
        file_id = msg.document.file_id
        file_type = "document"
    else:
        await msg.reply_text("Send a *photo* screenshot, or tap Skip.", parse_mode=ParseMode.MARKDOWN)
        return PAYMENT_PROOF

    ok = db.mark_order_awaiting_confirmation(
        int(oid), proof_file_id=file_id, proof_file_type=file_type
    )
    clear_awaiting(context)
    context.user_data.pop("proof_order_id", None)
    if not ok:
        await msg.reply_text("Could not update order status.")
        return ConversationHandler.END

    order = db.get_order(int(oid))
    items = db.get_order_items(int(oid))
    await msg.reply_text(
        f"✅ Screenshot received for order *#{oid}*.\n"
        "Seller has been notified and will confirm payment.\n\n"
        f"{db.format_order_summary(order, items, SYM)}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_main_kb(),
    )
    await _notify_admins_payment_claim(context, order, items)
    return ConversationHandler.END


async def cb_skip_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    oid = int(query.data.split(":")[1])
    user = update.effective_user
    order = db.get_order(oid)
    if not order or order["user_id"] != user.id:
        await query.answer("Not your order", show_alert=True)
        return ConversationHandler.END
    ok = db.mark_order_awaiting_confirmation(oid)
    clear_awaiting(context)
    context.user_data.pop("proof_order_id", None)
    if not ok and order["status"] != "awaiting_confirmation":
        await query.answer("Could not update", show_alert=True)
        return ConversationHandler.END
    await query.answer("Marked paid — awaiting confirmation")
    order = db.get_order(oid)
    items = db.get_order_items(oid)
    await safe_edit(
        query,
        f"✅ Got it — order *#{oid}* is awaiting confirmation.\n"
        "The seller will confirm once payment is received.\n\n"
        f"{db.format_order_summary(order, items, SYM)}",
        back_main_kb(),
    )
    await _notify_admins_payment_claim(context, order, items)
    return ConversationHandler.END


async def cb_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    oid = int(query.data.split(":")[1])
    user = update.effective_user
    ok, msg = db.cancel_order(oid, user.id)
    await query.answer(msg, show_alert=True)
    if ok:
        await safe_edit(query, f"❌ Order #{oid} cancelled.\nInventory unchanged.", back_main_kb())


async def cb_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    orders = db.list_user_orders(user.id, limit=15)
    if not orders:
        await safe_edit(query, "No orders yet.", back_main_kb())
        return
    lines = ["📦 *Your orders*\n"]
    buttons = []
    for o in orders:
        lines.append(
            f"#{o['id']} · {o['status']} · {money(o['total'])} · {o['created_at'][:10]}"
        )
        buttons.append(
            [InlineKeyboardButton(f"Order #{o['id']}", callback_data=f"vieword:{o['id']}")]
        )
    buttons.append([InlineKeyboardButton("« Menu", callback_data="main")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_view_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    oid = int(query.data.split(":")[1])
    order = db.get_order(oid)
    user = update.effective_user
    if not order:
        await query.answer("Not found", show_alert=True)
        return
    is_adm = db.is_admin(order["chat_id"], user.id)
    if order["user_id"] != user.id and not is_adm:
        await query.answer("Not allowed", show_alert=True)
        return
    items = db.get_order_items(oid)
    text = db.format_order_summary(order, items, SYM)
    buttons = []
    if order["user_id"] == user.id and order["status"] == "pending_payment":
        buttons.append(
            [InlineKeyboardButton("✅ I've paid", callback_data=f"paid:{oid}")]
        )
        buttons.append(
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancelord:{oid}")]
        )
    if is_adm and order["status"] in ("pending_payment", "awaiting_confirmation"):
        buttons.append(
            [
                InlineKeyboardButton(
                    "✅ Confirm + tracking", callback_data=f"admconfirm:{oid}"
                ),
                InlineKeyboardButton("❌ Reject", callback_data=f"admreject:{oid}"),
            ]
        )
        if order.get("payment_proof_file_id"):
            buttons.append(
                [
                    InlineKeyboardButton(
                        "🖼 View proof", callback_data=f"viewproof:{oid}"
                    )
                ]
            )
    if is_adm and order["status"] == "paid" and not (order.get("tracking_number") or "").strip():
        buttons.append(
            [
                InlineKeyboardButton(
                    "📦 Add tracking", callback_data=f"addtrack:{oid}"
                )
            ]
        )
    buttons.append([InlineKeyboardButton("« Back", callback_data="myorders")])
    await safe_edit(query, text, InlineKeyboardMarkup(buttons))


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_awaiting(context)
    context.user_data.pop("edit_pid", None)
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── Admin panel ──────────────────────────────────────────────────────────────


def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, bool]:
    user = update.effective_user
    sid = shop_id(context, update)
    if not user:
        return None, False
    if sid is None:
        # Try private shop / first admin shop
        shops = db.shops_for_admin(user.id)
        if shops:
            sid = shops[0]["chat_id"]
            set_shop(context, sid)
    if sid is None:
        return None, False
    return sid, db.is_admin(sid, user.id)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await update.message.reply_text("Admin only. Ask an owner to add you with /admin.")
        return
    await _admin_home(update, context, sid, edit=False)


async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "Admin only.")
        return ConversationHandler.END
    await _admin_home(update, context, sid, edit=True)
    return ConversationHandler.END


async def _admin_home(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sid: int, edit: bool
) -> None:
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    products = db.list_products(sid)
    pending = db.list_orders(sid, status="awaiting_confirmation", limit=50)
    open_pay = db.list_orders(sid, status="pending_payment", limit=50)
    text = (
        f"⚙️ *Admin — {shop['title']}*\n"
        f"Shop ID: `{sid}`\n"
        f"Products: {len(products)} · "
        f"Awaiting confirm: {len(pending)} · Pending pay: {len(open_pay)}\n\n"
        f"Shipping: {'ON' if shop.get('shipping_enabled') else 'OFF'} · "
        f"{money(shop['shipping_fee'])} · free over {money(shop['free_shipping_above'])}"
    )
    disp = db.shop_display(shop)
    if int(disp["min_order_qty"]) > 0:
        text += (
            f"\nMin order: {db.format_min_order_rule(disp['min_order_qty'], disp['min_order_label'])}"
        )
    else:
        text += "\nMin order: OFF"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 Products", callback_data="adm_prods"),
                InlineKeyboardButton("➕ Add product", callback_data="adm_addprod"),
            ],
            [
                InlineKeyboardButton("📥 Import inventory", callback_data="adm_import"),
                InlineKeyboardButton("📝 Mass edit", callback_data="adm_massedit"),
            ],
            [
                InlineKeyboardButton("📋 Orders", callback_data="adm_orders"),
                InlineKeyboardButton("⏳ Needs confirm", callback_data="adm_awaiting"),
            ],
            [
                InlineKeyboardButton("💳 Payments", callback_data="adm_pays"),
                InlineKeyboardButton("🚚 Shipping", callback_data="adm_ship"),
            ],
            [
                InlineKeyboardButton("📦 Min order (vial/kit)", callback_data="adm_minorder"),
            ],
            [
                InlineKeyboardButton("📤 Export Reports", callback_data="adm_export"),
            ],
            [
                InlineKeyboardButton("👥 Admins", callback_data="adm_admins"),
                InlineKeyboardButton("🔗 Shop link", callback_data="adm_link"),
            ],
            [
                InlineKeyboardButton("🤝 Collaborations", callback_data="adm_collab"),
            ],
            [
                InlineKeyboardButton("✏️ Rename shop", callback_data="adm_rename_shop"),
                InlineKeyboardButton("🚚 Move to group", callback_data="adm_transfer"),
            ],
            [
                InlineKeyboardButton("📋 Clone shop", callback_data="adm_clone"),
            ],
            [InlineKeyboardButton("« Menu", callback_data="main")],
        ]
    )
    # Master admin (OWNER_IDS) only — never show to shop staff
    if update.effective_user and db.is_owner(update.effective_user.id):
        rows = list(kb.inline_keyboard)
        rows.insert(
            -1,
            [InlineKeyboardButton("👑 Master fees / invoices", callback_data="master_home")],
        )
        rows.insert(
            -1,
            [
                InlineKeyboardButton(
                    "🗑 Clear inventory (owner)",
                    callback_data="owner_clear_inv",
                )
            ],
        )
        kb = InlineKeyboardMarkup(rows)
    # Note shared inventory on clone shops
    shop_full = shop
    if shop_full.get("inventory_master_chat_id"):
        text += (
            f"\n\n_Shared inventory from master_ `{shop_full['inventory_master_chat_id']}` "
            f"— prices are unique to this group."
        )
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cb_adm_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=shop_{sid}"
    await safe_edit(
        query,
        f"🔗 *Customer shop link*\n\n`{link}`\n\nShare this so buyers open the right catalog.",
        back_main_kb([[InlineKeyboardButton("« Admin", callback_data="admin")]]),
    )


async def cb_owner_clear_inv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """OWNER_IDS only: confirm screen before wiping shop catalog."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not db.is_owner(user.id):
        await safe_edit(
            query,
            "Bot owner only. Your Telegram ID must be in `OWNER_IDS`.",
            back_main_kb([[InlineKeyboardButton("« Admin", callback_data="admin")]]),
        )
        return
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "No shop selected. /start then Admin.", back_main_kb())
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    products = db.list_products(sid, active_only=False)
    n = len(products)
    await safe_edit(
        query,
        f"🗑 *Clear shop inventory* (owner only)\n\n"
        f"Shop: *{shop.get('title') or sid}* (`{sid}`)\n"
        f"Products to delete: *{n}*\n\n"
        "This *permanently deletes* all catalog products for this shop.\n"
        "• Orders history, payments, shipping, admins — *kept*\n"
        "• Product stock rows — *removed*\n"
        "• Collab shares for this catalog — cleaned\n\n"
        "_This cannot be undone (except restore from backup)._",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"✅ Yes, delete all {n} products",
                        callback_data="owner_clear_inv_yes",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin")],
            ]
        ),
    )


async def cb_owner_clear_inv_yes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """OWNER_IDS only: perform clear after confirmation."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not db.is_owner(user.id):
        await safe_edit(
            query,
            "Bot owner only.",
            back_main_kb([[InlineKeyboardButton("« Admin", callback_data="admin")]]),
        )
        return
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "No shop selected.", back_main_kb())
        return
    success, msg, deleted = db.clear_shop_inventory(int(sid), user.id)
    log.info(
        "owner_clear_inventory shop=%s by=%s ok=%s deleted=%s",
        sid,
        user.id,
        success,
        deleted,
    )
    prefix = "✅ " if success else "❌ "
    await safe_edit(
        query,
        prefix + msg,
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📦 Products", callback_data="adm_prods")],
                [InlineKeyboardButton("« Admin", callback_data="admin")],
            ]
        ),
    )


async def cb_adm_rename_shop_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Shop admin: rename this shop's display title."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "Admin only.", back_main_kb())
        return ConversationHandler.END
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    set_awaiting(context, "edit_shop_title")
    await query.message.reply_text(
        f"✏️ *Rename shop*\n\nCurrent name: *{shop['title']}*\n\n"
        "Send the new shop name (max 80 characters).\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("New shop name..."),
    )
    return EDIT_SHOP_TITLE


async def edit_shop_title_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await accept_prompt_message(update, context):
        return EDIT_SHOP_TITLE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    raw = update.message.text or ""
    success, result = db.rename_shop(
        int(sid), raw, by_user=update.effective_user.id if update.effective_user else None
    )
    if not success:
        await update.message.reply_text(result)
        return EDIT_SHOP_TITLE
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Shop renamed to *{result}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Admin", callback_data="admin")]]
        ),
    )
    return ConversationHandler.END


async def cb_adm_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin-only: create a one-time token to move this shop into another group."""
    query = update.callback_query
    await query.answer()
    try:
        sid, ok = _require_admin(update, context)
        if not ok or sid is None:
            await safe_edit(
                query,
                "Admin only, or no shop selected. Send /start and open Admin again.",
                back_main_kb(),
            )
            return ConversationHandler.END
        try:
            tok = db.create_transfer_token(sid, update.effective_user.id)
        except PermissionError:
            await safe_edit(query, "Admin only.", back_main_kb())
            return ConversationHandler.END
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={tok['deep_link_arg']}"
        shop = db.get_shop(sid)
        title = (shop or {}).get("title") or "Shop"
        await safe_edit(
            query,
            f"🚚 *Move shop to another group*\n\n"
            f"Shop: *{title}* (`{sid}`)\n\n"
            "Moves *everything* (catalog, stock, orders, payments, admins) "
            "to a new group. Old customer links keep working.\n\n"
            "This is *not* a clone — the shop leaves the old chat.\n\n"
            f"1. Add this bot to the destination group\n"
            f"2. Open this link (you):\n{link}\n"
            f"3. In the destination group send:\n"
            f"`/claim_transfer {tok['token']}`\n\n"
            "Destination must not already have products/orders.\n"
            "Any previous unused transfer token for this shop is cancelled.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Admin", callback_data="admin")]]
            ),
        )
    except Exception as exc:
        log.exception("cb_adm_transfer failed")
        await safe_edit(query, f"Transfer setup failed: {exc}", back_main_kb())
    return ConversationHandler.END


async def cb_adm_clone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin-only: create a one-time link to clone this shop into another group."""
    query = update.callback_query
    await query.answer()
    try:
        sid, ok = _require_admin(update, context)
        if not ok or sid is None:
            await safe_edit(
                query,
                "Admin only, or no shop selected. Send /start and open Admin again.",
                back_main_kb(),
            )
            return ConversationHandler.END
        import franchise

        franchise.ensure_franchise_tables()
        try:
            tok = franchise.create_clone_token(sid, update.effective_user.id)
        except PermissionError:
            await safe_edit(query, "Admin only.", back_main_kb())
            return ConversationHandler.END
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={tok['deep_link_arg']}"
        await safe_edit(
            query,
            "Clone this shop into another group\n\n"
            "Creates a new group shop with:\n"
            "- Separate prices (edit freely)\n"
            "- Same shared inventory as the master stock\n"
            "- Shipping/payments copied as a starting point\n\n"
            f"1. Add this bot to the new group\n"
            f"2. Open this link in Telegram (you):\n{link}\n"
            f"3. Then in the new group send:\n/claim_clone {tok['token']}\n\n"
            f"Master inventory shop: {tok['inventory_master_chat_id']}\n"
            "Only the admin who created the link can claim it.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Admin", callback_data="admin")]]
            ),
        )
    except Exception as exc:
        log.exception("cb_adm_clone failed")
        await safe_edit(query, f"Clone failed: {exc}", back_main_kb())
    return ConversationHandler.END


async def cmd_claim_clone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """In target group: /claim_clone <token> — attach clone (admin of source)."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text(
            "Run /claim_clone *inside the new group* where you want the cloned shop.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not context.args:
        await update.message.reply_text("Usage: `/claim_clone <token>`", parse_mode=ParseMode.MARKDOWN)
        return
    token = context.args[0].strip()
    import franchise

    franchise.ensure_franchise_tables()
    title = chat.title or "Shop"
    ok, msg = franchise.attach_clone(token, chat.id, user.id, title=title)
    await update.message.reply_text(
        ("✅ " if ok else "❌ ") + msg,
        parse_mode=ParseMode.MARKDOWN,
    )
    if ok:
        set_shop(context, chat.id)


async def cmd_claim_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """In destination group: /claim_transfer <token> — move shop here."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text(
            "Run /claim_transfer *inside the destination group* "
            "where you want this shop to live.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/claim_transfer <token>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    token = context.args[0].strip()
    title = chat.title or None
    ok, msg, new_id = db.transfer_shop_to_group(
        token, chat.id, user.id, title=title
    )
    await update.message.reply_text(
        ("✅ " if ok else "❌ ") + msg,
        parse_mode=ParseMode.MARKDOWN,
    )
    if ok and new_id is not None:
        set_shop(context, new_id)
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=shop_{new_id}"
        await update.message.reply_text(
            f"🔗 New customer shop link:\n`{link}`\n\n"
            "Share this with buyers. Old links still work via redirect.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")]]
            ),
        )


async def cmd_master(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """OWNER_IDS only: service fees + weekly invoices."""
    user = update.effective_user
    if not user or not db.is_owner(user.id):
        await update.message.reply_text("Master admin only.")
        return
    await _master_home(update, context, edit=False)


async def _master_home(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool
) -> None:
    import franchise

    franchise.ensure_franchise_tables()
    shops = franchise.list_shops_service_fees()
    open_inv = franchise.list_invoices(status="open", limit=10)
    lines = [
        "Master control (you only)\n",
        "Hidden service fee is folded into each order shipping total. "
        "Customers never see a separate line. Tracked for weekly invoices.\n",
        f"Shops: {len(shops)} · Open invoices: {len(open_inv)}\n",
    ]
    for s in shops[:15]:
        fee = float(s.get("hidden_service_fee") or 0)
        tag = " [clone]" if s.get("inventory_master_chat_id") else ""
        lines.append(
            f"- {s['chat_id']} {s.get('title') or 'Shop'}{tag} - fee {money(fee)}/order"
        )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Set service fee", callback_data="master_setfee")],
            [InlineKeyboardButton("Fee ledger", callback_data="master_ledger")],
            [InlineKeyboardButton("Generate weekly invoices", callback_data="master_geninv")],
            [InlineKeyboardButton("Open invoices", callback_data="master_invoices")],
            [InlineKeyboardButton("« Admin", callback_data="admin")],
        ]
    )
    text = "\n".join(lines)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)


async def cb_master_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        if not update.effective_user or not db.is_owner(update.effective_user.id):
            await safe_edit(
                query,
                "Master admin only. Your Telegram ID must be in OWNER_IDS on the server.",
                back_main_kb(),
            )
            return ConversationHandler.END
        await _master_home(update, context, edit=True)
    except Exception as exc:
        log.exception("cb_master_home failed")
        await safe_edit(query, f"Master panel failed: {exc}", back_main_kb())
    return ConversationHandler.END


async def cb_master_setfee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not update.effective_user or not db.is_owner(update.effective_user.id):
        await safe_edit(query, "Master admin only.")
        return
    import franchise

    shops = franchise.list_shops_service_fees()
    buttons = [
        [
            InlineKeyboardButton(
                f"{(s.get('title') or 'Shop')[:20]} `{s['chat_id']}`",
                callback_data=f"master_feeshop:{s['chat_id']}",
            )
        ]
        for s in shops[:30]
    ]
    buttons.append([InlineKeyboardButton("« Master", callback_data="master_home")])
    await safe_edit(
        query,
        "Pick a group/shop to set its *hidden service fee* (added into every order's shipping):",
        InlineKeyboardMarkup(buttons),
    )


async def cb_master_feeshop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not update.effective_user or not db.is_owner(update.effective_user.id):
        await safe_edit(query, "Master admin only.")
        return ConversationHandler.END
    chat_id = int(query.data.split(":")[1])
    context.user_data["master_fee_shop"] = chat_id
    import franchise

    fee = franchise.get_hidden_service_fee(chat_id)
    await query.message.reply_text(
        f"Send the new hidden service fee in dollars for shop `{chat_id}` "
        f"(current *{money(fee)}*).\nExample: `2.50`\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("e.g. 2.50"),
    )
    return MASTER_FEE_INPUT


async def master_fee_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not db.is_owner(update.effective_user.id):
        await update.message.reply_text("Master admin only.")
        return ConversationHandler.END
    raw = (update.message.text or "").strip().replace("$", "")
    try:
        fee = float(raw)
    except ValueError:
        await update.message.reply_text("Send a number like `3` or `2.50`.", parse_mode=ParseMode.MARKDOWN)
        return MASTER_FEE_INPUT
    chat_id = context.user_data.get("master_fee_shop")
    if not chat_id:
        await update.message.reply_text("Session expired. Open Master again.")
        return ConversationHandler.END
    import franchise

    ok, msg = franchise.set_hidden_service_fee(int(chat_id), fee, update.effective_user.id)
    context.user_data.pop("master_fee_shop", None)
    await update.message.reply_text(
        ("✅ " if ok else "❌ ") + msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Master", callback_data="master_home")]]
        ),
    )
    return ConversationHandler.END


async def cb_master_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not update.effective_user or not db.is_owner(update.effective_user.id):
        await safe_edit(query, "Master admin only.")
        return
    import franchise

    rows = franchise.service_fee_ledger(limit=25)
    if not rows:
        text = "No orders with service fees yet."
    else:
        lines = ["📊 *Service fee ledger* (recent)\n"]
        for r in rows:
            lines.append(
                f"• `{r.get('payment_code') or r['id']}` shop `{r['chat_id']}` "
                f"fee *{money(r['hidden_service_fee'])}* · {r['status']} · "
                f"{r.get('paid_at') or r.get('created_at')}"
            )
        text = "\n".join(lines)
    await safe_edit(
        query,
        text,
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Master", callback_data="master_home")]]
        ),
    )


async def cb_master_geninv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not update.effective_user or not db.is_owner(update.effective_user.id):
        await safe_edit(query, "Master admin only.")
        return
    import franchise

    ok, msg, invs = franchise.generate_weekly_invoices(update.effective_user.id)
    lines = [("✅ " if ok else "❌ ") + msg, ""]
    if not invs:
        lines.append("_No billable fees this week (or all zero)._")
    for i in invs:
        lines.append(
            f"• Invoice #{i['id']} `{i['chat_id']}` {i.get('title') or ''} — "
            f"*{money(i['total_fees'])}* across {i['order_count']} orders ({i['status']})"
        )
    await safe_edit(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📬 Open invoices", callback_data="master_invoices")],
                [InlineKeyboardButton("« Master", callback_data="master_home")],
            ]
        ),
    )


async def cb_master_invoices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not update.effective_user or not db.is_owner(update.effective_user.id):
        await safe_edit(query, "Master admin only.")
        return
    import franchise

    invs = franchise.list_invoices(status="open", limit=20)
    if not invs:
        text = "No open invoices. Generate weekly invoices first."
        buttons = [[InlineKeyboardButton("« Master", callback_data="master_home")]]
    else:
        lines = ["📬 *Open service-fee invoices*\n"]
        buttons = []
        for i in invs:
            lines.append(
                f"• #{i['id']} shop `{i['chat_id']}` {i.get('title') or ''} — "
                f"*{money(i['total_fees'])}* ({i['order_count']} orders)\n"
                f"  Week `{i['week_start']}` → `{i['week_end']}`"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Mark paid #{i['id']} ({money(i['total_fees'])})",
                        callback_data=f"master_invpaid:{i['id']}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton("« Master", callback_data="master_home")])
        text = "\n".join(lines)
    await safe_edit(query, text, InlineKeyboardMarkup(buttons))


async def cb_master_invpaid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not update.effective_user or not db.is_owner(update.effective_user.id):
        await safe_edit(query, "Master admin only.")
        return
    inv_id = int(query.data.split(":")[1])
    import franchise

    ok, msg = franchise.mark_invoice_paid(inv_id, update.effective_user.id)
    await query.answer(msg, show_alert=True)
    await cb_master_invoices(update, context)


async def cb_adm_collab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin: shop-to-shop inventory collaboration hub."""
    query = update.callback_query
    await query.answer()
    try:
        sid, ok = _require_admin(update, context)
        if not ok or sid is None:
            await safe_edit(
                query,
                "Admin only, or no shop selected. Send /start and open Admin again.",
                back_main_kb(),
            )
            return ConversationHandler.END
        import collab

        collab.ensure_collab_tables()
        accepted = collab.list_collaborations(sid)
        pending = collab.list_pending_invites(sid)
        shares = collab.list_shares(sid, active_only=False)
        settlements = collab.list_settlements(sid, as_host=True, status="owed")
        lines = [
            "Collaborations\n",
            "Invite another shop to share their stock on your catalog.",
            "You set markup %; customer pays you; you settle guest base cost.\n",
            f"Active partners: {len(accepted)} · Pending invites: {len(pending)}",
            f"Shared SKUs: {len([s for s in shares if s.get('active')])}",
            f"Owed to guest shops: {len(settlements)} settlements",
        ]
        if settlements:
            total_owed = sum(float(s["amount"]) for s in settlements)
            lines.append(f"Total owed: {money(total_owed)}")
        kb_rows = [
            [InlineKeyboardButton("➕ Create invite link", callback_data="collab_invite")],
            [InlineKeyboardButton("📦 Manage shared products", callback_data="collab_shares")],
            [InlineKeyboardButton("💸 Settlements (pay guests)", callback_data="collab_settle")],
            [InlineKeyboardButton("« Admin", callback_data="admin")],
        ]
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(kb_rows))
    except Exception as exc:
        log.exception("cb_adm_collab failed")
        await safe_edit(query, f"Collaborations failed: {exc}", back_main_kb())
    return ConversationHandler.END


async def cb_collab_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    import collab

    collab.ensure_collab_tables()
    inv = collab.create_invite(sid, update.effective_user.id, default_markup_pct=15)
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={inv['deep_link_arg']}"
    await safe_edit(
        query,
        f"🔗 *Guest shop invite*\n\n"
        f"Send this to the other shop's admin:\n`{link}`\n\n"
        f"Default markup when you share their items: *{inv['default_markup_pct']}%*\n"
        f"(You can change markup per product after they accept.)",
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Collaborations", callback_data="adm_collab")]]
        ),
    )


async def cb_collab_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # collab_accept:TOKEN:GUEST_CHAT_ID
    parts = query.data.split(":")
    if len(parts) != 3:
        await safe_edit(query, "Bad accept payload.")
        return
    _, token, guest_sid = parts
    import collab

    collab.ensure_collab_tables()
    ok, msg = collab.accept_invite(token, int(guest_sid), update.effective_user.id)
    await safe_edit(
        query,
        ("✅ " if ok else "❌ ") + msg,
        back_main_kb(),
    )


async def cb_collab_shares(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    import collab

    collab.ensure_collab_tables()
    partners = collab.list_collaborations(sid)
    if not partners:
        await safe_edit(
            query,
            "No accepted partners yet. Create an invite and have them accept.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Collaborations", callback_data="adm_collab")]]
            ),
        )
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"From: {p.get('guest_title') or p['guest_chat_id']}",
                callback_data=f"collab_guest:{p['guest_chat_id']}",
            )
        ]
        for p in partners
        if p.get("guest_chat_id")
    ]
    buttons.append([InlineKeyboardButton("« Collaborations", callback_data="adm_collab")])
    await safe_edit(
        query,
        "Pick a guest shop to choose products + markup:",
        InlineKeyboardMarkup(buttons),
    )


async def cb_collab_guest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    guest_id = int(query.data.split(":")[1])
    import collab

    collab.ensure_collab_tables()
    prods = collab.list_guest_products_for_host(sid, guest_id)
    if not prods:
        await safe_edit(
            query,
            "Guest shop has no active products.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data="collab_shares")]]
            ),
        )
        return
    lines = ["📦 *Guest products* — tap to toggle share / set markup\n"]
    buttons = []
    for p in prods[:30]:
        shared = bool(p.get("share_active"))
        mk = p.get("share_markup")
        mk_s = f"+{mk}%" if mk is not None and shared else "off"
        sell = float(p["price"]) * (1 + float(mk or 0) / 100) if shared else float(p["price"])
        lines.append(
            f"• {p['name']} · base {money(p['price'])} · stock {p['stock']} · {mk_s}"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if shared else '➕'} {p['name'][:18]} ({mk_s})",
                    callback_data=f"collab_tog:{guest_id}:{p['id']}",
                )
            ]
        )
        if shared:
            buttons.append(
                [
                    InlineKeyboardButton("10%", callback_data=f"collab_mk:{guest_id}:{p['id']}:10"),
                    InlineKeyboardButton("15%", callback_data=f"collab_mk:{guest_id}:{p['id']}:15"),
                    InlineKeyboardButton("25%", callback_data=f"collab_mk:{guest_id}:{p['id']}:25"),
                    InlineKeyboardButton("40%", callback_data=f"collab_mk:{guest_id}:{p['id']}:40"),
                ]
            )
    buttons.append([InlineKeyboardButton("« Shares", callback_data="collab_shares")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_collab_tog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    _, guest_id, pid = query.data.split(":")
    guest_id, pid = int(guest_id), int(pid)
    import collab

    collab.ensure_collab_tables()
    # Toggle: if active, disable; else enable at 15%
    shares = collab.list_shares(sid, active_only=False)
    existing = next((s for s in shares if int(s["product_id"]) == pid), None)
    if existing and existing.get("active"):
        collab.set_share(sid, guest_id, pid, float(existing.get("markup_pct") or 15), active=False)
        await query.answer("Share off", show_alert=False)
    else:
        collab.set_share(sid, guest_id, pid, 15.0, active=True)
        await query.answer("Shared at +15%", show_alert=False)
    # Re-render guest list
    query.data = f"collab_guest:{guest_id}"
    await cb_collab_guest(update, context)


async def cb_collab_mk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    _, guest_id, pid, mk = query.data.split(":")
    import collab

    collab.ensure_collab_tables()
    collab.set_share(sid, int(guest_id), int(pid), float(mk), active=True)
    await query.answer(f"Markup +{mk}%", show_alert=False)
    query.data = f"collab_guest:{guest_id}"
    await cb_collab_guest(update, context)


async def cb_collab_settle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    import collab

    collab.ensure_collab_tables()
    rows = collab.list_settlements(sid, as_host=True, status="owed")
    if not rows:
        await safe_edit(
            query,
            "No open settlements. When collab orders confirm paid, guest base amounts appear here.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Collaborations", callback_data="adm_collab")]]
            ),
        )
        return
    lines = ["💸 *Owed to guest shops*\n(Customer already paid you the full amount.)\n"]
    buttons = []
    for s in rows:
        title = s.get("guest_title") or s["guest_chat_id"]
        lines.append(
            f"• Order `{s.get('payment_code') or s['order_id']}` → {title}: *{money(s['amount'])}*"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Mark paid {money(s['amount'])} → {str(title)[:16]}",
                    callback_data=f"collab_paid:{s['id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("« Collaborations", callback_data="adm_collab")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_collab_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    sett_id = int(query.data.split(":")[1])
    import collab

    collab.ensure_collab_tables()
    ok2, msg = collab.mark_settlement_paid(sett_id, update.effective_user.id)
    await query.answer(msg, show_alert=True)
    await cb_collab_settle(update, context)


async def cb_adm_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin export submenu: inventory / pending / both."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "Admin only.")
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    await safe_edit(
        query,
        f"📤 *Export Reports — {shop['title']}*\n\n"
        "Generates a `.txt` file and sends it here (nothing saved on disk).",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📦 Inventory", callback_data="export:inv")],
                [InlineKeyboardButton("⏳ Pending Orders", callback_data="export:pending")],
                [InlineKeyboardButton("📋 Full Report (both)", callback_data="export:both")],
                [InlineKeyboardButton("« Admin", callback_data="admin")],
            ]
        ),
    )


async def cb_export_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build report in-memory and send as Telegram document."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await query.answer("Admin only", show_alert=True)
        return
    kind = (query.data or "").split(":")[-1]
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    title = reports.safe_filename_part(shop.get("title") or f"shop{sid}")
    day = datetime.now(timezone.utc).strftime("%Y%m%d")

    if kind == "inv":
        body = reports.generate_inventory_report(sid)
        filename = f"inventory_{title}_{day}.txt"
        caption = f"📦 Inventory — {shop['title']}"
    elif kind == "pending":
        body = reports.generate_pending_orders_report(sid)
        filename = f"pending_orders_{title}_{day}.txt"
        caption = f"⏳ Pending orders — {shop['title']}"
    elif kind == "both":
        body = reports.generate_full_report(sid)
        filename = f"full_report_{title}_{day}.txt"
        caption = f"📋 Full report — {shop['title']}"
    else:
        await query.answer("Unknown report", show_alert=True)
        return

    buf = io.BytesIO(body.encode("utf-8"))
    buf.name = filename
    chat_id = update.effective_chat.id if update.effective_chat else query.message.chat_id
    try:
        await context.bot.send_document(
            chat_id=chat_id,
            document=buf,
            filename=filename,
            caption=caption,
        )
        await query.answer("Report sent")
    except Exception as exc:
        log.warning("export send failed: %s", exc)
        await query.answer("Could not send report — try again in DM", show_alert=True)


# Products admin


async def cb_adm_import_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Admin: wait for a .txt inventory layout document."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "Admin only.", back_main_kb())
        return ConversationHandler.END
    set_awaiting(context, "import_inventory")
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 Download template", callback_data="adm_import_tpl")],
            [InlineKeyboardButton("« Products", callback_data="adm_prods")],
            [InlineKeyboardButton("« Admin", callback_data="admin")],
        ]
    )
    await safe_edit(
        query,
        "📥 *Import inventory* (add new only)\n\n"
        "Send a *`.txt` document* with one product per line:\n"
        "`name | price | stock | unit | description`\n\n"
        "Example:\n"
        "`Tren Ace | 45.00 | 10 | vial | acetate`\n"
        "`Test E | 30 | 5 | bottle |`\n\n"
        "• Lines starting with `#` are ignored\n"
        "• Existing product names are *skipped* (not updated)\n"
        "• For bulk *updates*, use *Mass edit* instead\n"
        "• Nothing is deleted\n\n"
        "Upload the file now, or download the template first.\n"
        "/cancel to abort",
        kb,
    )
    return IMPORT_INVENTORY_FILE


async def cb_adm_import_template(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Send a downloadable template; stay in import conversation."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return ConversationHandler.END
    set_awaiting(context, "import_inventory")
    buf = io.BytesIO(inventory_import.TEMPLATE_TEXT.encode("utf-8"))
    buf.name = "inventory_import_template.txt"
    await query.message.reply_document(
        document=buf,
        filename="inventory_import_template.txt",
        caption=(
            "Template ready. Edit in Notepad, then send the file back here "
            "(name | price | stock | unit | description)."
        ),
    )
    return IMPORT_INVENTORY_FILE


async def _read_inventory_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, conv_state: int
) -> tuple[str | None, int]:
    """Shared .txt download for import / mass edit. Returns (text, state) or (None, state)."""
    msg = update.message
    if not msg or not msg.document:
        if msg:
            await msg.reply_text(
                "Please send a `.txt` *document* (not a photo).",
                parse_mode=ParseMode.MARKDOWN,
            )
        return None, conv_state

    doc = msg.document
    fname = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    ok_type = (
        fname.endswith(".txt")
        or fname.endswith(".csv")
        or mime.startswith("text/")
        or mime in ("application/octet-stream", "")
    )
    if not ok_type:
        await msg.reply_text(
            "Please upload a plain text file (`.txt`).\n"
            f"Got: `{doc.file_name or 'unknown'}` ({doc.mime_type or 'no type'})",
            parse_mode=ParseMode.MARKDOWN,
        )
        return None, conv_state
    if doc.file_size and doc.file_size > inventory_import.MAX_FILE_BYTES:
        await msg.reply_text(
            f"File too large (max {inventory_import.MAX_FILE_BYTES // 1000} KB)."
        )
        return None, conv_state

    try:
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        text = inventory_import.decode_upload_bytes(data)
        return text, conv_state
    except ValueError as exc:
        await msg.reply_text(str(exc))
        return None, conv_state
    except Exception as exc:
        log.exception("inventory file download failed")
        await msg.reply_text(f"Could not read file: {exc}")
        return None, conv_state


async def on_import_inventory_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive .txt upload, parse, create products (add-only)."""
    if not await accept_prompt_message(update, context):
        return IMPORT_INVENTORY_FILE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    text, st = await _read_inventory_document(
        update, context, conv_state=IMPORT_INVENTORY_FILE
    )
    if text is None:
        return st

    parsed, imported = inventory_import.import_from_text(
        int(sid), text, mode="add_only"
    )
    clear_awaiting(context)
    summary = inventory_import.format_import_summary(
        parsed, imported, mode="add_only"
    )
    await update.message.reply_text(
        summary,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📦 Products", callback_data="adm_prods")],
                [InlineKeyboardButton("📥 Import again", callback_data="adm_import")],
                [InlineKeyboardButton("« Admin", callback_data="admin")],
            ]
        ),
    )
    return ConversationHandler.END


async def cb_adm_massedit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin mass edit hub: download catalog, choose upload mode."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "Admin only.", back_main_kb())
        return
    n = len(db.list_products(sid, active_only=False))
    text = (
        "📝 *Mass edit inventory*\n\n"
        f"Products in shop: *{n}*\n\n"
        "1. *Download* your catalog as a `.txt` file\n"
        "2. Edit in Notepad (price, stock, unit, description)\n"
        "3. Choose a mode and *upload* the file\n\n"
        "Format:\n"
        "`name | price | stock | unit | description`\n\n"
        "• Match by *product name* (case-insensitive)\n"
        "• Stock values are absolute (not +/−)\n"
        "• Units: vial, bottle, pack, kit, etc.\n"
        "• Nothing is deleted by this tool"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬇️ Download inventory", callback_data="mass_dl")],
            [
                InlineKeyboardButton(
                    "⬆️ Upload — update existing", callback_data="mass_up:update_only"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬆️ Upload — add + update", callback_data="mass_up:upsert"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬆️ Upload — add new only", callback_data="mass_up:add_only"
                )
            ],
            [InlineKeyboardButton("« Products", callback_data="adm_prods")],
            [InlineKeyboardButton("« Admin", callback_data="admin")],
        ]
    )
    await safe_edit(query, text, kb)


async def cb_mass_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    text = inventory_import.export_inventory_text(
        int(sid), active_only=False, shop_title=str(shop.get("title") or "shop")
    )
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe_title = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in str(shop.get("title") or "shop")
    )[:40]
    buf = io.BytesIO(text.encode("utf-8"))
    buf.name = f"inventory_{safe_title}_{day}.txt"
    await query.message.reply_document(
        document=buf,
        filename=buf.name,
        caption=(
            "Current inventory export.\n"
            "Edit, then Admin → Mass edit → choose upload mode and send this file back."
        ),
    )


async def cb_mass_upload_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Begin mass-edit file upload with a chosen mode."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await safe_edit(query, "Admin only.", back_main_kb())
        return ConversationHandler.END
    mode = (query.data or "").split(":")[-1]
    if mode not in ("add_only", "update_only", "upsert"):
        mode = "upsert"
    context.user_data["mass_edit_mode"] = mode
    set_awaiting(context, "mass_edit_file")
    labels = {
        "add_only": "Add new only (skip existing names)",
        "update_only": "Update existing only (skip unknown names)",
        "upsert": "Add new + update existing",
    }
    await query.message.reply_text(
        f"📝 *Mass edit — {labels[mode]}*\n\n"
        "Send your `.txt` document now.\n"
        "`name | price | stock | unit | description`\n\n"
        "/cancel to abort",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Send inventory .txt file..."),
    )
    return MASS_EDIT_FILE


async def on_mass_edit_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive mass-edit .txt and apply selected mode."""
    if not await accept_prompt_message(update, context):
        return MASS_EDIT_FILE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    text, st = await _read_inventory_document(
        update, context, conv_state=MASS_EDIT_FILE
    )
    if text is None:
        return st

    mode = context.user_data.get("mass_edit_mode") or "upsert"
    if mode not in ("add_only", "update_only", "upsert"):
        mode = "upsert"
    parsed, imported = inventory_import.import_from_text(
        int(sid), text, mode=mode  # type: ignore[arg-type]
    )
    clear_awaiting(context)
    context.user_data.pop("mass_edit_mode", None)
    summary = inventory_import.format_import_summary(
        parsed, imported, mode=mode  # type: ignore[arg-type]
    )
    await update.message.reply_text(
        summary,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Mass edit", callback_data="adm_massedit")],
                [InlineKeyboardButton("📦 Products", callback_data="adm_prods")],
                [InlineKeyboardButton("« Admin", callback_data="admin")],
            ]
        ),
    )
    return ConversationHandler.END


async def cb_adm_prods(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    products = db.list_products(sid)
    if not products:
        await safe_edit(
            query,
            "No products yet.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ Add product", callback_data="adm_addprod")],
                    [InlineKeyboardButton("📥 Import inventory", callback_data="adm_import")],
                    [InlineKeyboardButton("📝 Mass edit", callback_data="adm_massedit")],
                    [InlineKeyboardButton("« Admin", callback_data="admin")],
                ]
            ),
        )
        return
    lines = ["📦 *Products*\n"]
    buttons = []
    for p in products:
        flag = "✅" if p["active"] else "⏸"
        lines.append(
            f"{flag} #{p['id']} *{p['name']}* — {money(p['price'])} · stock {p['stock']}"
        )
        buttons.append(
            [InlineKeyboardButton(f"#{p['id']} {p['name'][:24]}", callback_data=f"admp:{p['id']}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("➕ Add", callback_data="adm_addprod"),
            InlineKeyboardButton("📥 Import", callback_data="adm_import"),
        ]
    )
    buttons.append(
        [InlineKeyboardButton("📝 Mass edit", callback_data="adm_massedit")]
    )
    buttons.append([InlineKeyboardButton("« Admin", callback_data="admin")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_adm_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if not p:
        await query.answer("Missing", show_alert=True)
        return
    has_file = db.product_has_coa_file(p)
    has_url = db.product_has_coa_url(p)
    coa_bits = []
    if has_file:
        ftype = (p.get("coa_file_type") or "file").strip()
        fname = (p.get("coa_filename") or "").strip()
        bit = f"file ({ftype}"
        if fname:
            bit += f" · {fname}"
        bit += ")"
        coa_bits.append(bit)
    if has_url:
        coa_bits.append(f"link `{p.get('coa_url')}`")
    if coa_bits:
        coa_line = "COA: ✅ " + " · ".join(coa_bits) + "\n"
    else:
        coa_line = "COA: _not set — upload PDF/photo or paste a link_\n"
    kp = db.product_kit_price(p)
    if kp is not None:
        kit_line = f"Kit of {KIT_SIZE}: {money(kp)}"
        if int(p.get("stock") or 0) < KIT_SIZE:
            kit_line += f" _(hidden from buyers until stock ≥ {KIT_SIZE})_"
        kit_line += "\n"
    else:
        kit_line = f"Kit of {KIT_SIZE}: _not set_\n"
    text = (
        f"*{p['name']}* (#{p['id']})\n"
        f"Price: {money(p['price'])} / {p.get('unit') or 'vial'}\n"
        f"Unit: {p.get('unit') or 'vial'}\n"
        f"{kit_line}"
        f"Stock: {p['stock']}\n"
        f"Active: {'yes' if p['active'] else 'no'}\n"
        f"{coa_line}"
        f"{p.get('description') or ''}"
    )
    rows = [
        [InlineKeyboardButton("✏️ Edit Name", callback_data=f"setname:{pid}")],
        [
            InlineKeyboardButton("💲 Price", callback_data=f"setprice:{pid}"),
            InlineKeyboardButton("📊 Stock", callback_data=f"setstock:{pid}"),
        ],
        [InlineKeyboardButton("📏 Unit", callback_data=f"setunit:{pid}")],
        [
            InlineKeyboardButton("📦 Kit price", callback_data=f"setkit:{pid}"),
            InlineKeyboardButton("🗑 Clear kit", callback_data=f"clearkit:{pid}"),
        ],
        [InlineKeyboardButton("📄 Set COA (file or link)", callback_data=f"setcoa:{pid}")],
    ]
    if has_file or has_url:
        coa_row = [InlineKeyboardButton("📄 Send COA", callback_data=f"viewcoa:{pid}")]
        if has_url:
            coa_row.append(
                InlineKeyboardButton("🔗 Open link", url=(p.get("coa_url") or "").strip())
            )
        coa_row.append(InlineKeyboardButton("🗑 Remove COA", callback_data=f"clearcoa:{pid}"))
        # Telegram allows max ~8 buttons/row; split if needed
        if len(coa_row) > 2:
            rows.append(coa_row[:2])
            rows.append(coa_row[2:])
        else:
            rows.append(coa_row)
    rows.append(
        [
            InlineKeyboardButton(
                "⏸ Deactivate" if p["active"] else "▶️ Activate",
                callback_data=f"togglep:{pid}",
            ),
            InlineKeyboardButton("🗑 Delete", callback_data=f"delp:{pid}"),
        ]
    )
    rows.append([InlineKeyboardButton("« Products", callback_data="adm_prods")])
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


async def cb_toggle_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok:
        await query.answer("Denied", show_alert=True)
        return
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if not p:
        await query.answer("Missing", show_alert=True)
        return
    db.update_product(pid, active=0 if p["active"] else 1)
    await query.answer("Updated")
    query.data = f"admp:{pid}"
    await cb_adm_product(update, context)


async def cb_del_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok:
        await query.answer("Denied", show_alert=True)
        return
    pid = int(query.data.split(":")[1])
    db.delete_product(pid)
    await query.answer("Deleted")
    await cb_adm_prods(update, context)


async def cb_set_coa_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return ConversationHandler.END
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if not p or int(p["chat_id"]) != int(sid):
        await query.answer("Product not in this shop", show_alert=True)
        return ConversationHandler.END
    context.user_data["edit_pid"] = pid
    set_awaiting(context, "product_coa")
    await query.message.reply_text(
        f"📄 *COA for {p['name']}*\n\n"
        "Send either:\n"
        "• a *PDF document* or *photo* of the COA, **or**\n"
        "• a *link* starting with `http://` or `https://`\n\n"
        "You can set both (upload one, then set the other later).\n"
        "Buyers tap 📄 COA to get the file and/or link.\n\n"
        "/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("PDF, photo, or https://..."),
    )
    return EDIT_COA_VALUE


async def edit_coa_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Accept PDF, photo, or external URL for product COA."""
    if not await accept_prompt_message(update, context):
        return EDIT_COA_VALUE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    pid = context.user_data.get("edit_pid")
    msg = update.message
    if not msg or pid is None:
        clear_awaiting(context)
        return ConversationHandler.END

    saved_kind = None  # "file" | "url"
    kb_extra = []

    if msg.document:
        doc = msg.document
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        is_pdf = mime == "application/pdf" or name.endswith(".pdf")
        if not is_pdf:
            await msg.reply_text(
                "Please send a *PDF* document, a *photo*, or a *https://* link.\n"
                f"(Got: `{doc.mime_type or 'unknown'}`)",
                parse_mode=ParseMode.MARKDOWN,
            )
            return EDIT_COA_VALUE
        success, result = db.set_product_coa_file(
            int(pid), int(sid), doc.file_id, "document", doc.file_name
        )
        if not success:
            await msg.reply_text(result)
            return EDIT_COA_VALUE
        saved_kind = "file"
    elif msg.photo:
        success, result = db.set_product_coa_file(
            int(pid), int(sid), msg.photo[-1].file_id, "photo", None
        )
        if not success:
            await msg.reply_text(result)
            return EDIT_COA_VALUE
        saved_kind = "file"
    elif msg.text and msg.text.strip():
        raw = msg.text.strip()
        success, result = db.set_product_coa_url(int(pid), int(sid), raw)
        if not success:
            await msg.reply_text(result)
            return EDIT_COA_VALUE
        saved_kind = "url"
        kb_extra.append([InlineKeyboardButton("🔗 Open link", url=result)])
    else:
        await msg.reply_text(
            "Send a PDF, photo, or https:// link for the COA. /cancel"
        )
        return EDIT_COA_VALUE

    clear_awaiting(context)
    p = db.get_product(int(pid))
    name = p["name"] if p else f"#{pid}"
    if saved_kind == "url":
        body = (
            f"✅ COA *link* saved for *{name}*.\n"
            f"`{result}`\n\n"
            "Buyers can open it from 📄 COA (Telegram in-app browser may block some lab sites)."
        )
    else:
        body = (
            f"✅ COA *file* saved for *{name}*.\n"
            "Buyers can tap 📄 COA to receive it in chat."
        )
    kb_rows = [
        [InlineKeyboardButton("📄 Send COA", callback_data=f"viewcoa:{pid}")],
        *kb_extra,
        [InlineKeyboardButton("« Product", callback_data=f"admp:{pid}")],
        [InlineKeyboardButton("« Products", callback_data="adm_prods")],
    ]
    await msg.reply_text(
        body,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_rows),
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


async def cb_view_coa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send COA file and/or share external COA link when user taps 📄 COA."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if not p:
        await query.answer("Product not found", show_alert=True)
        return
    sid = shop_id(context, update)
    is_adm = db.is_admin(int(p["chat_id"]), user.id)
    # Buyers: product must be in current shop catalog (own or collab share).
    # Shop admin of the product's shop may open even if inactive.
    if not is_adm:
        if _catalog_entry_for_shop(sid, pid) is None:
            await query.answer("COA not available in this shop", show_alert=True)
            return
        if not p.get("active"):
            await query.answer("Product unavailable", show_alert=True)
            return
    elif not p.get("active") and not is_adm:
        await query.answer("Product unavailable", show_alert=True)
        return

    file_id = (p.get("coa_file_id") or "").strip()
    coa_url = (p.get("coa_url") or "").strip()
    if not file_id and not coa_url:
        await query.answer(
            "No COA set. Admin: Set COA (file or link).",
            show_alert=True,
        )
        return

    sent_any = False
    errors: list[str] = []

    if file_id:
        ftype = (p.get("coa_file_type") or "document").strip().lower()
        caption = f"📄 COA — {p['name']}"
        try:
            if ftype == "photo":
                await context.bot.send_photo(
                    chat_id=chat.id,
                    photo=file_id,
                    caption=caption,
                )
            else:
                await context.bot.send_document(
                    chat_id=chat.id,
                    document=file_id,
                    caption=caption,
                    filename=p.get("coa_filename") or None,
                )
            sent_any = True
        except Exception as e:
            log.warning("send COA file failed product=%s: %s", pid, e)
            errors.append("file")

    if coa_url:
        try:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔗 Open COA link", url=coa_url)]]
            )
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"📄 *COA link — {p['name']}*\n\n"
                    f"`{coa_url}`\n\n"
                    "_If the site blocks Telegram’s browser, long-press the link "
                    "and open in Safari/Chrome._"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            sent_any = True
        except Exception as e:
            log.warning("send COA url failed product=%s: %s", pid, e)
            errors.append("link")

    if sent_any:
        await query.answer("COA sent")
    else:
        await query.answer(
            "Could not send COA. Admin: re-set file/link on the product.",
            show_alert=True,
        )


async def cb_clear_coa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await query.answer("Denied", show_alert=True)
        return
    pid = int(query.data.split(":")[1])
    if not db.clear_product_coa(pid, sid):
        await query.answer("Not found", show_alert=True)
        return
    await query.answer("COA removed")
    query.data = f"admp:{pid}"
    await cb_adm_product(update, context)


async def cb_set_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if not p or int(p["chat_id"]) != int(sid):
        await query.answer("Product not in this shop", show_alert=True)
        return ConversationHandler.END
    context.user_data["edit_pid"] = pid
    set_awaiting(context, "edit_name")
    old = p["name"]
    await query.message.reply_text(
        f"✏️ *Edit name*\n\nCurrent: *{old}*\n\n"
        f"Send the new name for this product.\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("New product name..."),
    )
    return EDIT_NAME_VALUE


async def edit_name_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return EDIT_NAME_VALUE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    pid = context.user_data.get("edit_pid")
    raw = update.message.text or ""
    success, result = db.rename_product(int(pid), int(sid), raw)
    if not success:
        # Keep state so admin can retry
        await update.message.reply_text(result)
        return EDIT_NAME_VALUE
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Renamed to *{result}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("« Product", callback_data=f"admp:{pid}")],
                [InlineKeyboardButton("« Products", callback_data="adm_prods")],
            ]
        ),
    )
    return ConversationHandler.END


async def cb_set_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if p and sid is not None and int(p["chat_id"]) != int(sid):
        await query.answer("Product not in this shop", show_alert=True)
        return ConversationHandler.END
    context.user_data["edit_pid"] = pid
    set_awaiting(context, "edit_price")
    await query.message.reply_text(
        f"Send new price for product #{pid} (e.g. `45.00`)\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Price e.g. 45.00"),
    )
    return EDIT_PRICE_VALUE


async def cb_set_kit_price_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if not p or (sid is not None and int(p["chat_id"]) != int(sid)):
        await query.answer("Product not in this shop", show_alert=True)
        return ConversationHandler.END
    context.user_data["edit_pid"] = pid
    set_awaiting(context, "edit_kit_price")
    cur = db.product_kit_price(p)
    cur_s = money(cur) if cur is not None else "not set"
    await query.message.reply_text(
        f"📦 *Kit price for {p['name']}*\n\n"
        f"One kit = *{KIT_SIZE}* vials/stock units.\n"
        f"Current: {cur_s}\n\n"
        f"Send the price for a full kit of {KIT_SIZE} "
        f"(e.g. `90.00`).\n"
        f"Kit option is *hidden* from buyers when stock is below {KIT_SIZE}.\n"
        f"Send `0` to remove kit pricing.\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply(f"Kit of {KIT_SIZE} price e.g. 90"),
    )
    return EDIT_KIT_PRICE


async def edit_kit_price_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return EDIT_KIT_PRICE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    pid = context.user_data.get("edit_pid")
    p = db.get_product(int(pid)) if pid else None
    if not p or int(p["chat_id"]) != int(sid):
        clear_awaiting(context)
        await update.message.reply_text("Product not found in this shop.")
        return ConversationHandler.END
    raw = (update.message.text or "").replace("$", "").strip()
    try:
        price = float(raw)
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid. Send a price (e.g. 90) or 0 to clear.\n/cancel",
            reply_markup=force_reply(f"Kit of {KIT_SIZE} price e.g. 90"),
        )
        return EDIT_KIT_PRICE
    ok_set, msg = db.set_product_kit_price(int(pid), int(sid), None if price == 0 else price)
    clear_awaiting(context)
    await update.message.reply_text(
        f"{'✅' if ok_set else '❌'} {msg}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Product", callback_data=f"admp:{pid}")]]
        ),
    )
    return ConversationHandler.END


async def cb_clear_kit_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await query.answer("Denied", show_alert=True)
        return
    pid = int(query.data.split(":")[1])
    ok_set, msg = db.set_product_kit_price(pid, sid, None)
    await query.answer(msg if ok_set else "Failed", show_alert=not ok_set)
    query.data = f"admp:{pid}"
    await cb_adm_product(update, context)


async def edit_price_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return EDIT_PRICE_VALUE
    sid, ok = _require_admin(update, context)
    if not ok:
        clear_awaiting(context)
        return ConversationHandler.END
    try:
        price = float((update.message.text or "").replace("$", "").strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid price. Try again or /cancel")
        return EDIT_PRICE_VALUE
    pid = context.user_data.get("edit_pid")
    p = db.get_product(int(pid)) if pid else None
    if not p or (sid is not None and int(p["chat_id"]) != int(sid)):
        clear_awaiting(context)
        await update.message.reply_text("Product not found in this shop.")
        return ConversationHandler.END
    db.update_product(pid, price=price)
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Price set to {money(price)}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Product", callback_data=f"admp:{pid}")]]
        ),
    )
    return ConversationHandler.END


async def cb_set_stock_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if p and sid is not None and int(p["chat_id"]) != int(sid):
        await query.answer("Product not in this shop", show_alert=True)
        return ConversationHandler.END
    context.user_data["edit_pid"] = pid
    set_awaiting(context, "edit_stock")
    await query.message.reply_text(
        f"Send new *absolute* stock for #{pid}, or `+5` / `-2` to adjust.\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Stock qty or +5 / -2"),
    )
    return EDIT_STOCK_VALUE


async def edit_stock_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return EDIT_STOCK_VALUE
    sid, ok = _require_admin(update, context)
    if not ok:
        clear_awaiting(context)
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    pid = context.user_data.get("edit_pid")
    actor = update.effective_user.id if update.effective_user else None
    p = db.get_product(int(pid)) if pid else None
    if not p or (sid is not None and int(p["chat_id"]) != int(sid)):
        clear_awaiting(context)
        await update.message.reply_text("Product not found in this shop.")
        return ConversationHandler.END
    try:
        if raw.startswith(("+", "-")) and raw[1:].isdigit():
            new = db.adjust_stock(
                pid, int(raw), actor_id=actor, reason="admin_stock_delta"
            )
        else:
            stock = int(raw)
            if stock < 0:
                raise ValueError
            before = int(p["stock"])
            delta = stock - before
            new = db.adjust_stock(
                pid, delta, actor_id=actor, reason="admin_stock_set"
            )
            if new is None:
                raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid. Send a number or +N / -N. /cancel")
        return EDIT_STOCK_VALUE
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Stock is now {new}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Product", callback_data=f"admp:{pid}")]]
        ),
    )
    return ConversationHandler.END


async def cb_add_prod_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    context.user_data["new_prod"] = {}
    set_awaiting(context, "add_prod")
    await query.message.reply_text(
        "➕ *New product*\n\nSend product name:\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Product name..."),
    )
    return ADD_PROD_NAME


async def add_prod_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_PROD_NAME
    context.user_data.setdefault("new_prod", {})["name"] = (update.message.text or "").strip()
    set_awaiting(context, "add_prod_price")
    await update.message.reply_text(
        "Price? (e.g. `39.99`)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Price e.g. 39.99"),
    )
    return ADD_PROD_PRICE


async def add_prod_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_PROD_PRICE
    try:
        price = float((update.message.text or "").replace("$", "").strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid price. Try again.",
            reply_markup=force_reply("Price e.g. 39.99"),
        )
        return ADD_PROD_PRICE
    context.user_data["new_prod"]["price"] = price
    set_awaiting(context, "add_prod_stock")
    await update.message.reply_text(
        "Starting stock quantity? (integer)",
        reply_markup=force_reply("Stock quantity..."),
    )
    return ADD_PROD_STOCK


async def add_prod_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_PROD_STOCK
    try:
        stock = int((update.message.text or "").strip())
        if stock < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid stock. Try again.",
            reply_markup=force_reply("Stock quantity..."),
        )
        return ADD_PROD_STOCK
    context.user_data["new_prod"]["stock"] = stock
    set_awaiting(context, "add_prod_unit")
    await update.message.reply_text(
        "Unit name? (e.g. `vial`, `bottle`, `pack`, `kit`)\n"
        "Send `-` for default *vial*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("vial / bottle / pack …"),
    )
    return ADD_PROD_UNIT


async def add_prod_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_PROD_UNIT
    raw = (update.message.text or "").strip()
    unit = inventory_import.normalize_unit(raw)
    context.user_data.setdefault("new_prod", {})["unit"] = unit
    set_awaiting(context, "add_prod_desc")
    await update.message.reply_text(
        "Short description? (or `-` for none)",
        reply_markup=force_reply("Description or -"),
    )
    return ADD_PROD_DESC


async def add_prod_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_PROD_DESC
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    desc = (update.message.text or "").strip()
    if desc == "-":
        desc = ""
    np = context.user_data.get("new_prod") or {}
    unit = inventory_import.normalize_unit(np.get("unit"))
    pid = db.add_product(
        sid,
        name=np["name"],
        price=np["price"],
        stock=np["stock"],
        description=desc,
        unit=unit,
    )
    context.user_data.pop("new_prod", None)
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Added *{np['name']}* (#{pid}) — {money(np['price'])} / {unit}, "
        f"stock {np['stock']}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("« Products", callback_data="adm_prods")],
                [InlineKeyboardButton("« Admin", callback_data="admin")],
            ]
        ),
    )
    return ConversationHandler.END


async def cb_set_unit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    pid = int(query.data.split(":")[1])
    p = db.get_product(pid)
    if not p or (sid is not None and int(p["chat_id"]) != int(sid)):
        await query.answer("Product not in this shop", show_alert=True)
        return ConversationHandler.END
    context.user_data["edit_pid"] = pid
    set_awaiting(context, "edit_unit")
    cur = p.get("unit") or "vial"
    await query.message.reply_text(
        f"📏 *Unit for {p['name']}*\n\n"
        f"Current: `{cur}`\n"
        f"Send new unit (e.g. vial, bottle, pack, kit).\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Unit e.g. bottle"),
    )
    return EDIT_UNIT_VALUE


async def edit_unit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return EDIT_UNIT_VALUE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    pid = context.user_data.get("edit_pid")
    p = db.get_product(int(pid)) if pid else None
    if not p or int(p["chat_id"]) != int(sid):
        clear_awaiting(context)
        await update.message.reply_text("Product not found in this shop.")
        return ConversationHandler.END
    unit = inventory_import.normalize_unit(update.message.text or "")
    db.update_product(int(pid), unit=unit)
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Unit set to `{unit}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Product", callback_data=f"admp:{pid}")]]
        ),
    )
    return ConversationHandler.END


# Orders admin


async def cb_adm_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    orders = db.list_orders(sid, limit=25)
    if not orders:
        await safe_edit(
            query,
            "No orders yet.",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Admin", callback_data="admin")]]),
        )
        return
    lines = ["📋 *Recent orders*\n"]
    buttons = []
    for o in orders:
        lines.append(f"#{o['id']} · `{o['status']}` · {money(o['total'])}")
        buttons.append(
            [InlineKeyboardButton(f"#{o['id']} {o['status']}", callback_data=f"vieword:{o['id']}")]
        )
    buttons.append([InlineKeyboardButton("« Admin", callback_data="admin")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_adm_awaiting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    orders = db.list_orders(sid, status="awaiting_confirmation", limit=30)
    pending = db.list_orders(sid, status="pending_payment", limit=30)
    lines = ["⏳ *Needs confirmation*\n"]
    buttons = []
    for o in orders:
        lines.append(f"#{o['id']} reported paid · {money(o['total'])}")
        buttons.append(
            [
                InlineKeyboardButton(f"✅ #{o['id']}", callback_data=f"admconfirm:{o['id']}"),
                InlineKeyboardButton(f"❌ #{o['id']}", callback_data=f"admreject:{o['id']}"),
            ]
        )
    if pending:
        lines.append("\n*Still pending payment:*")
        for o in pending:
            lines.append(f"#{o['id']} · {money(o['total'])}")
            buttons.append(
                [InlineKeyboardButton(f"View #{o['id']}", callback_data=f"vieword:{o['id']}")]
            )
    if not orders and not pending:
        lines.append("_Nothing waiting._")
    buttons.append([InlineKeyboardButton("« Admin", callback_data="admin")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_adm_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start confirm flow: ask for tracking, then confirm payment + notify customer."""
    query = update.callback_query
    user = update.effective_user
    oid = int(query.data.split(":")[1])
    order = db.get_order(oid)
    if not order or not db.is_admin(order["chat_id"], user.id):
        await query.answer("Not allowed", show_alert=True)
        return ConversationHandler.END
    if order["status"] not in ("pending_payment", "awaiting_confirmation"):
        await query.answer(f"Status: {order['status']}", show_alert=True)
        return ConversationHandler.END
    context.user_data["confirm_order_id"] = oid
    set_awaiting(context, "tracking_input")
    code = (order.get("payment_code") or "—").strip()
    await query.answer()
    await query.message.reply_text(
        f"✅ *Confirm payment — order #{oid}*\n"
        f"Payment code: `{code}` · Total: {money(order['total'])}\n\n"
        "Send *tracking number* (and optional carrier on the same line, e.g. `1Z999 USPS`).\n"
        "Or send `-` to confirm *without* tracking (you can add tracking later).\n"
        "/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Tracking or - ..."),
    )
    return TRACKING_INPUT


async def tracking_input_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Complete payment confirm with optional tracking; push status to customer."""
    if not await accept_prompt_message(update, context):
        return TRACKING_INPUT
    user = update.effective_user
    oid = context.user_data.get("confirm_order_id")
    raw = (update.message.text or "").strip()
    if not oid or not user:
        clear_awaiting(context)
        return ConversationHandler.END

    order = db.get_order(int(oid))
    if not order or not db.is_admin(order["chat_id"], user.id):
        await update.message.reply_text("Not allowed or order missing.")
        clear_awaiting(context)
        return ConversationHandler.END

    tracking = None
    carrier = None
    if raw and raw != "-":
        # First token = tracking; remainder optional carrier
        parts = raw.split(None, 1)
        tracking = parts[0]
        carrier = parts[1] if len(parts) > 1 else None

    ok, msg, low_alerts = db.confirm_order_payment(
        int(oid),
        user.id,
        tracking_number=tracking,
        tracking_carrier=carrier,
    )
    clear_awaiting(context)
    context.user_data.pop("confirm_order_id", None)
    order = db.get_order(int(oid))
    items = db.get_order_items(int(oid))
    await update.message.reply_text(
        f"{'✅' if ok else '⚠️'} {msg}\n\n{db.format_order_summary(order, items, SYM)}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Orders", callback_data="adm_orders")]]
        ),
    )
    if ok:
        _schedule_paid_backup()
    if ok and order:
        # Franchisee: local admins only until remittance proof is sent to main
        is_fr = _is_franchisee_order(order)
        sale_buttons = [
            [InlineKeyboardButton(f"View #{oid}", callback_data=f"vieword:{oid}")],
            [InlineKeyboardButton("📋 Orders", callback_data="adm_orders")],
        ]
        if is_fr:
            sale_buttons.insert(
                0,
                [
                    InlineKeyboardButton(
                        "📤 Send proof to main shop",
                        callback_data=f"frproof:{oid}",
                    )
                ],
            )
            headline = (
                "SALE CONFIRMED (paid) — franchisee only\n"
                "Main shop has NOT been notified yet.\n"
                "After you transfer/remit, tap Send proof to main shop."
            )
        else:
            headline = "SALE CONFIRMED (paid)"
        await _notify_admins_sale_report(
            context,
            order,
            items,
            headline=headline,
            reply_markup=InlineKeyboardMarkup(sale_buttons),
            audience="local" if is_fr else "auto",
        )
        if is_fr:
            await update.message.reply_text(
                "This is a *franchisee* sale.\n"
                "Main shop will *not* see it until you:\n"
                "1) Confirmed customer payment (done)\n"
                "2) Send *proof of payment to main shop* "
                f"(button below or /admin → order #{oid})\n",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📤 Send proof to main shop",
                                callback_data=f"frproof:{oid}",
                            )
                        ]
                    ]
                ),
            )
        # Push confirmation (+ tracking) to customer
        cust = (
            f"✅ *Payment confirmed* for order *#{oid}*\n"
            f"Total: {money(order['total'])}\n"
            f"Ship to:\n{order.get('ship_name') or '—'}\n"
            f"{order.get('ship_address') or '—'}\n"
        )
        track = (order.get("tracking_number") or "").strip()
        if track:
            car = (order.get("tracking_carrier") or "").strip()
            cust += f"\n📦 *Tracking:* `{track}`"
            if car:
                cust += f"\nCarrier: {car}"
            cust += "\n"
        else:
            cust += "\nWe'll send tracking when your package ships.\n"
        cust += "\nThank you!"
        try:
            await context.bot.send_message(
                order["user_id"], cust, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            log.info("Could not notify customer %s: %s", order["user_id"], e)
        if low_alerts:
            await _notify_low_stock(context, order["chat_id"], low_alerts)
    return ConversationHandler.END


async def cb_add_tracking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Add tracking to an already-paid order."""
    query = update.callback_query
    user = update.effective_user
    oid = int(query.data.split(":")[1])
    order = db.get_order(oid)
    if not order or not db.is_admin(order["chat_id"], user.id):
        await query.answer("Not allowed", show_alert=True)
        return ConversationHandler.END
    if order["status"] != "paid":
        await query.answer("Order must be paid first", show_alert=True)
        return ConversationHandler.END
    context.user_data["confirm_order_id"] = oid
    context.user_data["tracking_only"] = True
    set_awaiting(context, "tracking_input")
    await query.answer()
    await query.message.reply_text(
        f"📦 *Add tracking for order #{oid}*\n\n"
        "Send tracking number (optional carrier after a space).\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Tracking..."),
    )
    return TRACKING_INPUT


async def tracking_only_or_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Route tracking input: paid-only update vs full payment confirm."""
    if context.user_data.get("tracking_only"):
        if not await accept_prompt_message(update, context):
            return TRACKING_INPUT
        user = update.effective_user
        oid = context.user_data.get("confirm_order_id")
        raw = (update.message.text or "").strip()
        context.user_data.pop("tracking_only", None)
        if not oid or not user or not raw or raw == "-":
            clear_awaiting(context)
            await update.message.reply_text("Cancelled or empty tracking.")
            return ConversationHandler.END
        order = db.get_order(int(oid))
        if not order or not db.is_admin(order["chat_id"], user.id):
            clear_awaiting(context)
            await update.message.reply_text("Not allowed.")
            return ConversationHandler.END
        parts = raw.split(None, 1)
        tn, car = parts[0], (parts[1] if len(parts) > 1 else None)
        db.set_order_tracking(int(oid), tn, car)
        clear_awaiting(context)
        context.user_data.pop("confirm_order_id", None)
        order = db.get_order(int(oid))
        await update.message.reply_text(
            f"✅ Tracking saved for order *#{oid}*: `{tn}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            msg = f"📦 *Tracking update — order #{oid}*\n`{tn}`"
            if car:
                msg += f"\nCarrier: {car}"
            await context.bot.send_message(
                order["user_id"], msg, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
        return ConversationHandler.END
    return await tracking_input_value(update, context)


async def cb_view_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    oid = int(query.data.split(":")[1])
    order = db.get_order(oid)
    if not order or not db.is_admin(order["chat_id"], user.id):
        await query.answer("Not allowed", show_alert=True)
        return
    fid = (order.get("payment_proof_file_id") or "").strip()
    if not fid:
        await query.answer("No proof on file", show_alert=True)
        return
    await query.answer()
    ftype = (order.get("payment_proof_file_type") or "photo").lower()
    cap = f"Proof — order #{oid} · code {order.get('payment_code') or '—'}"
    try:
        if ftype == "document":
            await context.bot.send_document(
                query.message.chat_id, document=fid, caption=cap
            )
        else:
            await context.bot.send_photo(
                query.message.chat_id, photo=fid, caption=cap
            )
    except Exception as e:
        await query.answer(f"Could not send: {e}", show_alert=True)


async def cb_adm_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    oid = int(query.data.split(":")[1])
    order = db.get_order(oid)
    if not order or not db.is_admin(order["chat_id"], user.id):
        await query.answer("Not allowed", show_alert=True)
        return
    ok, msg = db.reject_order(oid, user.id)
    await query.answer(msg, show_alert=True)
    order = db.get_order(oid)
    items = db.get_order_items(oid)
    await safe_edit(
        query,
        f"{'❌' if ok else '⚠️'} {msg}\n\n{db.format_order_summary(order, items, SYM)}",
        InlineKeyboardMarkup([[InlineKeyboardButton("« Orders", callback_data="adm_orders")]]),
    )
    if ok:
        try:
            await context.bot.send_message(
                order["user_id"],
                f"❌ Order #{oid} was rejected by the shop. Inventory was not charged. "
                "Contact the seller if you already paid.",
            )
        except Exception:
            pass


# Payment methods admin


async def cb_adm_pays(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    methods = db.list_payment_methods(sid, active_only=False)
    lines = ["💳 *Payment methods*\n"]
    buttons = []
    for m in methods:
        flag = "✅" if m["active"] else "⏸"
        lines.append(f"{flag} #{m['id']} *{m['name']}*")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{'⏸' if m['active'] else '▶️'} {m['name'][:20]}",
                    callback_data=f"togglem:{m['id']}",
                ),
                InlineKeyboardButton("🗑", callback_data=f"delm:{m['id']}"),
            ]
        )
    if not methods:
        lines.append("_None configured._")
    lines.append("\n_Quick add:_")
    buttons.append(
        [
            InlineKeyboardButton("➕ Cash App", callback_data="paytpl:cashapp"),
            InlineKeyboardButton("➕ Venmo", callback_data="paytpl:venmo"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("➕ Crypto", callback_data="paytpl:crypto"),
            InlineKeyboardButton("➕ Zelle", callback_data="paytpl:zelle"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("➕ Custom", callback_data="paytpl:custom"),
            InlineKeyboardButton("➕ Freeform", callback_data="adm_addpay"),
        ]
    )
    buttons.append([InlineKeyboardButton("« Admin", callback_data="admin")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_toggle_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok:
        await query.answer("Denied", show_alert=True)
        return
    mid = int(query.data.split(":")[1])
    m = db.get_payment_method(mid)
    if not m:
        await query.answer("Missing", show_alert=True)
        return
    db.update_payment_method(mid, active=0 if m["active"] else 1)
    await query.answer("Updated")
    await cb_adm_pays(update, context)


async def cb_del_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok:
        await query.answer("Denied", show_alert=True)
        return
    mid = int(query.data.split(":")[1])
    db.delete_payment_method(mid)
    await query.answer("Deleted")
    await cb_adm_pays(update, context)


async def cb_add_pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    set_awaiting(context, "add_pay_name")
    await query.message.reply_text(
        "➕ Payment method name (e.g. `Cash App`, `Zelle`, `BTC`):\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Payment method name..."),
    )
    return ADD_PAY_NAME


async def cb_pay_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin one-tap Cash App / Venmo / Crypto / Zelle / Custom."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return ConversationHandler.END
    mt = (query.data or "").split(":")[-1]
    if mt not in pt.METHOD_TYPES:
        return ConversationHandler.END
    context.user_data["pay_tpl_type"] = mt
    context.user_data["pay_tpl_answers"] = []
    set_awaiting(context, f"pay_tpl_{mt}")
    prompts = pt.template_prompts(mt)
    placeholders = {
        "cashapp": "$Cashtag...",
        "venmo": "@Venmo handle...",
        "crypto": "Coin e.g. USDT...",
        "zelle": "Zelle email or phone...",
        "custom": "Payment instructions...",
    }
    await query.message.reply_text(
        prompts[0] + "\n\n/cancel",
        reply_markup=force_reply(placeholders.get(mt, "Type your answer...")),
    )
    return PAY_TPL_DETAILS


async def pay_tpl_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return PAY_TPL_DETAILS
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    mt = context.user_data.get("pay_tpl_type") or "custom"
    answers: list = context.user_data.setdefault("pay_tpl_answers", [])
    answers.append((update.message.text or "").strip())
    context.user_data["pay_tpl_answers"] = answers
    prompts = pt.template_prompts(mt)
    if len(answers) < len(prompts):
        next_ph = {
            1: "Wallet address...",
            2: "Network note or - ...",
        }.get(len(answers), "Type your answer...")
        if mt == "crypto":
            pass
        else:
            next_ph = "Type your answer..."
        if mt == "crypto" and len(answers) == 1:
            next_ph = "Wallet address..."
        elif mt == "crypto" and len(answers) == 2:
            next_ph = "Network note or - ..."
        set_awaiting(context, f"pay_tpl_{mt}")
        await update.message.reply_text(
            prompts[len(answers)],
            reply_markup=force_reply(next_ph),
        )
        return PAY_TPL_DETAILS
    payload = pt.render_from_answers(mt, answers)
    mid = db.add_payment_from_template(sid, payload)
    context.user_data.pop("pay_tpl_type", None)
    context.user_data.pop("pay_tpl_answers", None)
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Added *{payload['name']}* (#{mid})\n\n"
        f"{payload['instructions']}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Payments", callback_data="adm_pays")]]
        ),
    )
    return ConversationHandler.END


async def add_pay_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_PAY_NAME
    context.user_data["new_pay_name"] = (update.message.text or "").strip()
    set_awaiting(context, "add_pay_instr")
    await update.message.reply_text(
        "Payment instructions (handle, address, memo rules — multi-line OK):",
        reply_markup=force_reply("Payment instructions..."),
    )
    return ADD_PAY_INSTR


async def add_pay_instr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_PAY_INSTR
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    name = context.user_data.pop("new_pay_name", "Payment")
    instr = (update.message.text or "").strip()
    mid = db.add_payment_method(sid, name, instr, method_type="custom")
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Added payment method *{name}* (#{mid})",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Payments", callback_data="adm_pays")]]
        ),
    )
    return ConversationHandler.END


# Shipping admin


async def cb_adm_minorder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: vial/kit minimum order rule."""
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    disp = db.shop_display(shop)
    qty = int(disp["min_order_qty"])
    label = str(disp["min_order_label"])
    if qty > 0:
        status = db.format_min_order_rule(qty, label)
    else:
        status = "OFF (no minimum)"
    text = (
        "📦 *Minimum order rule*\n\n"
        f"Current: *{status}*\n\n"
        "Counts *total quantity* across all products in the cart "
        "(e.g. 1× Sema + 1× BPC = 2).\n"
        "Buyers cannot checkout until the minimum is met.\n\n"
        "Pick a unit type, set the number, or turn the rule off."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Unit: vial{' ✓' if label == 'vial' else ''}",
                    callback_data="minord_lab:vial",
                ),
                InlineKeyboardButton(
                    f"Unit: kit{' ✓' if label == 'kit' else ''}",
                    callback_data="minord_lab:kit",
                ),
            ],
            [
                InlineKeyboardButton("Set qty: 2", callback_data="minord_q:2"),
                InlineKeyboardButton("Set qty: 3", callback_data="minord_q:3"),
                InlineKeyboardButton("Set qty: 5", callback_data="minord_q:5"),
            ],
            [InlineKeyboardButton("Custom quantity…", callback_data="minord_custom")],
            [InlineKeyboardButton("Turn OFF", callback_data="minord_q:0")],
            [InlineKeyboardButton("« Admin", callback_data="admin")],
        ]
    )
    await safe_edit(query, text, kb)


async def cb_minord_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await query.answer("Denied", show_alert=True)
        return
    lab = query.data.split(":")[1].strip().casefold()
    if lab not in ("vial", "kit"):
        await query.answer("Invalid", show_alert=True)
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    qty = int(db.shop_display(shop)["min_order_qty"])
    ok_set, msg = db.set_min_order(sid, qty if qty > 0 else 0, label=lab)
    # If rule was off, only update label for next enable
    if qty <= 0:
        db.update_shop(sid, min_order_label=lab)
        await query.answer(f"Unit → {lab}")
    else:
        await query.answer(msg if ok_set else "Failed", show_alert=not ok_set)
    await cb_adm_minorder(update, context)


async def cb_minord_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await query.answer("Denied", show_alert=True)
        return
    try:
        qty = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.answer("Invalid", show_alert=True)
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    label = str(db.shop_display(shop)["min_order_label"])
    ok_set, msg = db.set_min_order(sid, qty, label=label)
    await query.answer(msg if ok_set else "Failed", show_alert=not ok_set)
    await cb_adm_minorder(update, context)


async def cb_minord_custom_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    set_awaiting(context, "min_order_qty")
    await query.message.reply_text(
        "Send minimum order quantity as a whole number "
        "(e.g. `2` for 2 vials/kits). Use `0` to disable.\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Min qty e.g. 2"),
    )
    return MIN_ORDER_QTY


async def min_order_qty_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return MIN_ORDER_QTY
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    try:
        qty = int(raw)
    except ValueError:
        await update.message.reply_text(
            "Send a whole number (e.g. 2) or /cancel",
            reply_markup=force_reply("Min qty e.g. 2"),
        )
        return MIN_ORDER_QTY
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    label = str(db.shop_display(shop)["min_order_label"])
    ok_set, msg = db.set_min_order(sid, qty, label=label)
    clear_awaiting(context)
    if not ok_set:
        await update.message.reply_text(
            f"❌ {msg}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Min order", callback_data="adm_minorder")]]
            ),
        )
        return ConversationHandler.END
    await update.message.reply_text(
        f"✅ {msg}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Min order", callback_data="adm_minorder")]]
        ),
    )
    return ConversationHandler.END


async def cb_adm_ship(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    text = (
        f"🚚 *Shipping settings*\n\n"
        f"Enabled: {'yes' if shop.get('shipping_enabled') else 'no'}\n"
        f"Flat fee: {money(shop['shipping_fee'])}\n"
        f"Free above: {money(shop['free_shipping_above'])} "
        f"(0 = never free)\n"
        f"Label: {shop.get('shipping_label') or 'Standard shipping'}\n\n"
        "Shipping is auto-added at checkout based on cart subtotal."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Toggle ON/OFF",
                    callback_data="ship_toggle",
                )
            ],
            [InlineKeyboardButton("Set flat fee", callback_data="ship_fee")],
            [InlineKeyboardButton("Set free-shipping threshold", callback_data="ship_free")],
            [InlineKeyboardButton("« Admin", callback_data="admin")],
        ]
    )
    await safe_edit(query, text, kb)


async def cb_ship_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await query.answer("Denied", show_alert=True)
        return
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    db.update_shop(sid, shipping_enabled=0 if shop.get("shipping_enabled") else 1)
    await query.answer("Toggled")
    await cb_adm_ship(update, context)


async def cb_ship_fee_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    set_awaiting(context, "ship_fee")
    await query.message.reply_text(
        "Send flat shipping fee (e.g. `8.00`):\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Shipping fee e.g. 8.00"),
    )
    return SHIP_FEE


async def ship_fee_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return SHIP_FEE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    try:
        fee = float((update.message.text or "").replace("$", "").strip())
        if fee < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid. Try again or /cancel",
            reply_markup=force_reply("Shipping fee e.g. 8.00"),
        )
        return SHIP_FEE
    db.update_shop(sid, shipping_fee=fee)
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Shipping fee set to {money(fee)}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Shipping", callback_data="adm_ship")]]
        ),
    )
    return ConversationHandler.END


async def cb_ship_free_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    set_awaiting(context, "ship_free")
    await query.message.reply_text(
        "Send free-shipping threshold subtotal (e.g. `150`). "
        "Use `0` to disable free shipping.\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Free ship over e.g. 150"),
    )
    return SHIP_FREE


async def ship_free_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return SHIP_FREE
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    try:
        val = float((update.message.text or "").replace("$", "").strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid. Try again or /cancel",
            reply_markup=force_reply("Free ship over e.g. 150"),
        )
        return SHIP_FREE
    db.update_shop(sid, free_shipping_above=val)
    clear_awaiting(context)
    await update.message.reply_text(
        f"✅ Free shipping above {money(val)}" if val > 0 else "✅ Free shipping disabled",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Shipping", callback_data="adm_ship")]]
        ),
    )
    return ConversationHandler.END


# Admins management


async def cb_adm_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        return
    admins = db.list_admins(sid)
    lines = [
        "👥 *Shop admins*\n",
        f"Global owners (env): {', '.join(str(i) for i in sorted(OWNER_IDS)) or 'none'}\n",
    ]
    buttons = []
    for a in admins:
        un = f"@{a['username']}" if a.get("username") else "—"
        lines.append(f"• `{a['user_id']}` {un}")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Remove {a['user_id']}", callback_data=f"rmadmin:{a['user_id']}"
                )
            ]
        )
    buttons.append([InlineKeyboardButton("➕ Add admin", callback_data="adm_addadmin")])
    buttons.append([InlineKeyboardButton("« Admin", callback_data="admin")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_rm_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        await query.answer("Denied", show_alert=True)
        return
    # Only owners can remove admins (or self-removal allowed)
    target = int(query.data.split(":")[1])
    if not db.is_owner(user.id) and target != user.id:
        await query.answer("Only owners can remove other admins", show_alert=True)
        return
    db.remove_admin(sid, target)
    await query.answer("Removed")
    await cb_adm_admins(update, context)


async def cb_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    sid, ok = _require_admin(update, context)
    if not ok:
        return ConversationHandler.END
    if not db.is_owner(user.id) and not ok:
        await safe_edit(query, "Only owners/admins can add.")
        return ConversationHandler.END
    set_awaiting(context, "add_admin")
    await query.message.reply_text(
        "Send the new admin's numeric Telegram *user ID*.\n"
        "They can get it from @userinfobot.\n/cancel",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=force_reply("Telegram user ID..."),
    )
    return ADD_ADMIN_ID


async def add_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await accept_prompt_message(update, context):
        return ADD_ADMIN_ID
    sid, ok = _require_admin(update, context)
    if not ok or sid is None:
        clear_awaiting(context)
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    if not raw.isdigit():
        await update.message.reply_text(
            "Need a numeric user ID. Try again or /cancel",
            reply_markup=force_reply("Telegram user ID..."),
        )
        return ADD_ADMIN_ID
    uid = int(raw)
    db.ensure_shop(sid)
    db.add_admin(sid, uid, None, update.effective_user.id)
    clear_awaiting(context)
    try:
        await context.bot.send_message(
            uid,
            f"You were added as an admin for shop `{sid}` on *{BRAND_NAME}*.\n"
            "Open the bot and use /admin.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Added admin `{uid}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Admins", callback_data="adm_admins")]]
        ),
    )
    return ConversationHandler.END


# Help


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = (
        f"*{BRAND_NAME} — Help*\n\n"
        "*Customers*\n"
        "• Browse Catalog → add items → Cart → Checkout\n"
        "• Choose payment method, enter shipping details\n"
        "• Shipping fee is auto-added (free over threshold if set)\n"
        "• Some shops require a *vial/kit minimum* (shown on home & cart)\n"
        "• Products may offer a *kit of 10* at kit price (hidden if stock < 10)\n"
        "• Pay using the instructions, then tap *I've paid*\n"
        "• Inventory only drops after an admin confirms payment\n\n"
        "*Admins* (`/admin`)\n"
        "• Products: add, price, unit, kit price, stock\n"
        "• *Mass edit*: download/upload `.txt` to bulk update\n"
        "• Orders: confirm/reject payments\n"
        "• Payment methods, shipping, and *min order (vial/kit)*\n"
        "• Add other admins per shop\n\n"
        "*Commands*\n"
        "/start /catalog /cart /orders /admin /cancel /help"
    )
    await safe_edit(query, text, back_main_kb())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sid = shop_id(context, update)
    await update.message.reply_text(
        f"*{brand_for(sid)}*\nUse the menu buttons or /start.\n"
        "Customers: /catalog /cart /myorders\n"
        "Admins: /admin /orders\n"
        "_Stock only drops after an admin confirms payment._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(
            db.is_admin(sid or 0, update.effective_user.id)
            if update.effective_user
            else False
        ),
    )


async def _send_user_order_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE, limit: int = 15
) -> None:
    user = update.effective_user
    orders = db.list_user_orders(user.id, limit=limit)
    if not orders:
        await update.message.reply_text("No orders yet.", reply_markup=main_menu_kb())
        return
    lines = ["📦 *Your orders*\n"]
    buttons = []
    for o in orders:
        lines.append(f"#{o['id']} · {o['status']} · {money(o['total'])}")
        buttons.append(
            [InlineKeyboardButton(f"Order #{o['id']}", callback_data=f"vieword:{o['id']}")]
        )
    buttons.append([InlineKeyboardButton("« Menu", callback_data="main")])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _send_admin_order_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sid: int, limit: int = 20
) -> None:
    orders = db.list_orders(sid, status=None, limit=limit)
    shop = db.get_shop(sid) or db.ensure_shop(sid)
    if not orders:
        await update.message.reply_text(
            f"No orders yet for *{shop['title']}*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(True),
        )
        return
    lines = [f"📋 *Orders — {shop['title']}* (last {len(orders)})\n"]
    buttons = []
    for o in orders:
        uname = o.get("username") or o.get("full_name") or o["user_id"]
        lines.append(
            f"#{o['id']} · `{o['status']}` · {money(o['total'])} · {uname}"
        )
        buttons.append(
            [InlineKeyboardButton(f"#{o['id']} {o['status']}", callback_data=f"vieword:{o['id']}")]
        )
    buttons.append([InlineKeyboardButton("« Admin", callback_data="admin")])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_myorders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buyer order history."""
    await _send_user_order_history(update, context)


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admins: shop order history. Buyers: fall back to personal orders."""
    user = update.effective_user
    sid, ok = _require_admin(update, context)
    if ok and sid is not None:
        await _send_admin_order_history(update, context, sid)
        return
    await _send_user_order_history(update, context)


async def _notify_low_stock(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, alerts: list[dict]
) -> None:
    if not alerts:
        return
    lines = ["⚠️ *Low stock alert*\n"]
    for a in alerts:
        lines.append(
            f"• *{a['name']}* — {a['stock']} left (threshold ≤ {a['threshold']})"
        )
    text = "\n".join(lines)
    recipients: set[int] = set(OWNER_IDS)
    for a in db.list_admins(chat_id):
        recipients.add(int(a["user_id"]))
    for uid in recipients:
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


def _schedule_paid_backup() -> None:
    """Encrypt live DB after stock-changing paid confirm (best-effort)."""
    try:
        path = backup_mod.maybe_backup_after_event(DB_PATH, reason="paid_confirm")
        if path:
            log.info("Post-paid backup ok: %s", path)
    except Exception as exc:
        log.exception("Post-paid backup error: %s", exc)


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: write encrypted snapshot now."""
    user = update.effective_user
    if not user or not db.is_owner(user.id):
        if update.message:
            await update.message.reply_text("Owners only.")
        return
    if not BACKUP_PASSPHRASE:
        await update.message.reply_text(
            "BACKUP_PASSPHRASE is not set on the host.\n"
            "Set it in env, then /backup again.\n"
            f"Vault dir: `{BACKUP_DIR}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        path = backup_mod.create_encrypted_backup(
            DB_PATH,
            BACKUP_DIR,
            BACKUP_PASSPHRASE,
            reason="manual",
        )
        pruned = backup_mod.prune_old_backups(BACKUP_DIR, BACKUP_RETENTION_DAYS)
        await update.message.reply_text(
            f"✅ Encrypted backup written.\n"
            f"File: `{path.name}`\n"
            f"Also updated: `latest.enc`\n"
            f"Dir: `{BACKUP_DIR}`\n"
            f"Retention: {BACKUP_RETENTION_DAYS} days (pruned {pruned} old file(s)).\n\n"
            f"Copy `latest.enc` to your laptop vault regularly.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.exception("Manual backup failed: %s", exc)
        await update.message.reply_text(f"Backup failed: {exc}")


async def cmd_backup_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: list recent vault files + token pool info."""
    user = update.effective_user
    if not user or not db.is_owner(user.id):
        if update.message:
            await update.message.reply_text("Owners only.")
        return
    files = backup_mod.list_backups(BACKUP_DIR)[:12]
    lines = [
        "*Backup / standby status*",
        f"Vault: `{BACKUP_DIR}`",
        f"Passphrase set: {'yes' if BACKUP_PASSPHRASE else 'NO'}",
        f"Retention days: {BACKUP_RETENTION_DAYS}",
        f"Failover: {'on' if TOKEN_FAILOVER else 'off'}",
    ]
    tokens = resolve_bot_tokens()
    lines.append(f"Token pool size: {len(tokens)}")
    if PUBLIC_BOT_USERNAME:
        lines.append(f"Public bot: @{PUBLIC_BOT_USERNAME}")
    if RECOVERY_URL:
        lines.append(f"Recovery URL: {RECOVERY_URL}")
    if files:
        lines.append("\n*Recent snapshots:*")
        for p in files:
            try:
                sz = p.stat().st_size
                lines.append(f"• `{p.name}` ({sz} bytes)")
            except OSError:
                lines.append(f"• `{p.name}`")
    else:
        lines.append("\n_No `.enc` files in vault yet._")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def post_init(app: Application) -> None:
    cmds = [
        BotCommand("start", "Open shop menu"),
        BotCommand("catalog", "Browse products"),
        BotCommand("search", "Search products"),
        BotCommand("cart", "View cart"),
        BotCommand("myorders", "Your order history"),
        BotCommand("orders", "Orders (admin shop / your orders)"),
        BotCommand("admin", "Admin panel"),
        BotCommand("setup", "Guided shop setup (group admins)"),
        BotCommand("claim_clone", "Attach cloned shop to this group"),
        BotCommand("claim_transfer", "Move shop into this group"),
        BotCommand("master", "Master fees/invoices (owner only)"),
        BotCommand("backup", "Encrypted DB snapshot (owner)"),
        BotCommand("backup_status", "Vault + token pool (owner)"),
        BotCommand("help", "Help"),
        BotCommand("cancel", "Cancel current step"),
    ]
    await app.bot.set_my_commands(cmds)


def build_app(token: str | None = None) -> Application:
    """Build PTB application for a specific bot token (standby pool aware)."""
    tokens = resolve_bot_tokens()
    use_token = (token or "").strip()
    if not use_token:
        if not tokens:
            raise SystemExit(
                "No bot token configured. Set TELEGRAM_BOT_TOKEN or BOT_TOKENS."
            )
        idx = token_pool.resolve_active_index(
            tokens, ACTIVE_BOT_INDEX, TOKEN_STATE_PATH
        )
        use_token = tokens[idx]
        log.info(
            "Using token pool index=%s fingerprint=%s pool_size=%s",
            idx,
            token_pool.token_fingerprint(use_token),
            len(tokens),
        )

    db.init_db()
    version = db.get_schema_version()
    log.info(
        "DB ready path=%s schema_version=%s",
        db.get_db_path(),
        version,
    )

    app = (
        Application.builder()
        .token(use_token)
        .post_init(post_init)
        .build()
    )
    app.bot_data["bot_token"] = use_token
    app.bot_data["token_fingerprint"] = token_pool.token_fingerprint(use_token)

    # Conversations (admin + checkout)
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_checkout_start, pattern=r"^checkout$"),
            CallbackQueryHandler(cb_customer_paid, pattern=r"^paid:\d+$"),
            CallbackQueryHandler(cb_adm_confirm, pattern=r"^admconfirm:\d+$"),
            CallbackQueryHandler(cb_add_tracking_start, pattern=r"^addtrack:\d+$"),
            CallbackQueryHandler(cb_add_prod_start, pattern=r"^adm_addprod$"),
            CallbackQueryHandler(cb_set_price_start, pattern=r"^setprice:\d+$"),
            CallbackQueryHandler(cb_set_kit_price_start, pattern=r"^setkit:\d+$"),
            CallbackQueryHandler(cb_set_stock_start, pattern=r"^setstock:\d+$"),
            CallbackQueryHandler(cb_set_unit_start, pattern=r"^setunit:\d+$"),
            CallbackQueryHandler(cb_set_name_start, pattern=r"^setname:\d+$"),
            CallbackQueryHandler(cb_set_coa_start, pattern=r"^setcoa:\d+$"),
            CallbackQueryHandler(cb_add_pay_start, pattern=r"^adm_addpay$"),
            CallbackQueryHandler(cb_pay_template_start, pattern=r"^paytpl:(cashapp|venmo|crypto|zelle|custom)$"),
            CallbackQueryHandler(cb_ship_fee_start, pattern=r"^ship_fee$"),
            CallbackQueryHandler(cb_ship_free_start, pattern=r"^ship_free$"),
            CallbackQueryHandler(cb_minord_custom_start, pattern=r"^minord_custom$"),
            CallbackQueryHandler(cb_add_admin_start, pattern=r"^adm_addadmin$"),
            CallbackQueryHandler(cb_search, pattern=r"^search$"),
            CallbackQueryHandler(cb_master_feeshop, pattern=r"^master_feeshop:-?\d+$"),
            CallbackQueryHandler(cb_franchise_proof_start, pattern=r"^frproof:\d+$"),
            CallbackQueryHandler(cb_adm_rename_shop_start, pattern=r"^adm_rename_shop$"),
            CallbackQueryHandler(cb_adm_import_start, pattern=r"^adm_import$"),
            CallbackQueryHandler(cb_mass_upload_start, pattern=r"^mass_up:(add_only|update_only|upsert)$"),
            CommandHandler("search", cmd_search),
        ],
        states={
            MASTER_FEE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, master_fee_input),
            ],
            FRANCHISE_PROOF: [
                MessageHandler(filters.PHOTO, franchise_proof_message),
                MessageHandler(filters.Document.ALL, franchise_proof_message),
                MessageHandler(filters.TEXT & ~filters.COMMAND, franchise_proof_message),
            ],
            EDIT_SHOP_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shop_title_value),
            ],
            IMPORT_INVENTORY_FILE: [
                MessageHandler(filters.Document.ALL, on_import_inventory_document),
                CallbackQueryHandler(cb_adm_import_template, pattern=r"^adm_import_tpl$"),
                CallbackQueryHandler(cb_adm_import_start, pattern=r"^adm_import$"),
            ],
            MASS_EDIT_FILE: [
                MessageHandler(filters.Document.ALL, on_mass_edit_document),
                CallbackQueryHandler(cb_adm_massedit, pattern=r"^adm_massedit$"),
            ],
            CHECKOUT_NAME: [
                CallbackQueryHandler(cb_pay_method, pattern=r"^paym:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_name),
            ],
            CHECKOUT_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_address),
            ],
            CHECKOUT_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_notes),
            ],
            CHECKOUT_VERIFY: [
                CallbackQueryHandler(cb_ship_ok, pattern=r"^shipok$"),
                CallbackQueryHandler(cb_ship_edit, pattern=r"^shipedit$"),
            ],
            PAYMENT_PROOF: [
                MessageHandler(filters.PHOTO, payment_proof_photo),
                MessageHandler(filters.Document.ALL, payment_proof_photo),
                CallbackQueryHandler(cb_skip_proof, pattern=r"^skipproof:\d+$"),
            ],
            TRACKING_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tracking_only_or_confirm),
            ],
            ADD_PROD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_prod_name),
            ],
            ADD_PROD_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_prod_price),
            ],
            ADD_PROD_STOCK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_prod_stock),
            ],
            ADD_PROD_UNIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_prod_unit),
            ],
            ADD_PROD_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_prod_desc),
            ],
            EDIT_PRICE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_price_value),
            ],
            EDIT_KIT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_kit_price_value),
            ],
            EDIT_STOCK_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_stock_value),
            ],
            EDIT_UNIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_unit_value),
            ],
            EDIT_NAME_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name_value),
            ],
            EDIT_COA_VALUE: [
                MessageHandler(filters.Document.ALL, edit_coa_value),
                MessageHandler(filters.PHOTO, edit_coa_value),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_coa_value),
            ],
            ADD_PAY_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pay_name),
            ],
            ADD_PAY_INSTR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pay_instr),
            ],
            PAY_TPL_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pay_tpl_details),
            ],
            SHIP_FEE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ship_fee_value),
            ],
            SHIP_FREE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ship_free_value),
            ],
            MIN_ORDER_QTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, min_order_qty_value),
            ],
            ADD_ADMIN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_id),
            ],
            SEARCH_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_search_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CallbackQueryHandler(cb_cart, pattern=r"^cart$"),
            CallbackQueryHandler(cb_main, pattern=r"^main$"),
            CallbackQueryHandler(cb_catalog, pattern=r"^cat$"),
            CallbackQueryHandler(cb_admin, pattern=r"^admin$"),
            # Admin nav must work even if a prior prompt left the user mid-conversation
            CallbackQueryHandler(cb_adm_collab, pattern=r"^adm_collab$"),
            CallbackQueryHandler(cb_adm_clone, pattern=r"^adm_clone$"),
            CallbackQueryHandler(cb_adm_transfer, pattern=r"^adm_transfer$"),
            CallbackQueryHandler(cb_adm_rename_shop_start, pattern=r"^adm_rename_shop$"),
            CallbackQueryHandler(cb_adm_import_start, pattern=r"^adm_import$"),
            CallbackQueryHandler(cb_adm_massedit, pattern=r"^adm_massedit$"),
            CallbackQueryHandler(cb_master_home, pattern=r"^master_home$"),
            CallbackQueryHandler(cb_master_setfee, pattern=r"^master_setfee$"),
            CallbackQueryHandler(cb_master_ledger, pattern=r"^master_ledger$"),
            CallbackQueryHandler(cb_master_geninv, pattern=r"^master_geninv$"),
            CallbackQueryHandler(cb_master_invoices, pattern=r"^master_invoices$"),
            CallbackQueryHandler(cb_adm_prods, pattern=r"^adm_prods$"),
            CallbackQueryHandler(cb_adm_orders, pattern=r"^adm_orders$"),
            CallbackQueryHandler(cb_adm_link, pattern=r"^adm_link$"),
            CallbackQueryHandler(cb_adm_export, pattern=r"^adm_export$"),
            CallbackQueryHandler(cb_adm_minorder, pattern=r"^adm_minorder$"),
            CallbackQueryHandler(cb_collab_invite, pattern=r"^collab_invite$"),
            CallbackQueryHandler(cb_collab_shares, pattern=r"^collab_shares$"),
            CallbackQueryHandler(cb_collab_settle, pattern=r"^collab_settle$"),
        ],
        allow_reentry=True,
        name="main_conv",
        persistent=False,
        conversation_timeout=AWAITING_TTL_SEC,
    )

    # Guided group setup (any group admin) — before generic handlers
    app.add_handler(setup_wizard.build_setup_conversation())
    app.add_handler(
        ChatMemberHandler(setup_wizard.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Admin / collab / master navigation FIRST (group 0) so ConversationHandler
    # never swallows these clicks when a user is mid-prompt.
    NAV = 0
    CONV = 1
    app.add_handler(CallbackQueryHandler(cb_adm_collab, pattern=r"^adm_collab$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_adm_clone, pattern=r"^adm_clone$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_adm_transfer, pattern=r"^adm_transfer$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_franchise_proof_start, pattern=r"^frproof:\d+$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_master_home, pattern=r"^master_home$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_owner_clear_inv, pattern=r"^owner_clear_inv$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_owner_clear_inv_yes, pattern=r"^owner_clear_inv_yes$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_master_setfee, pattern=r"^master_setfee$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_master_ledger, pattern=r"^master_ledger$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_master_geninv, pattern=r"^master_geninv$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_master_invoices, pattern=r"^master_invoices$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_master_invpaid, pattern=r"^master_invpaid:\d+$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_invite, pattern=r"^collab_invite$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_accept, pattern=r"^collab_accept:"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_shares, pattern=r"^collab_shares$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_guest, pattern=r"^collab_guest:"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_tog, pattern=r"^collab_tog:"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_mk, pattern=r"^collab_mk:"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_settle, pattern=r"^collab_settle$"), group=NAV)
    app.add_handler(CallbackQueryHandler(cb_collab_paid, pattern=r"^collab_paid:"), group=NAV)

    app.add_handler(conv, group=CONV)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("catalog", cmd_catalog))
    app.add_handler(CommandHandler("cart", cmd_cart))
    app.add_handler(CommandHandler("myorders", cmd_myorders))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("claim_clone", cmd_claim_clone))
    app.add_handler(CommandHandler("claim_transfer", cmd_claim_transfer))
    app.add_handler(CommandHandler("master", cmd_master))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("backup_status", cmd_backup_status))

    app.add_handler(CallbackQueryHandler(cb_pickshop, pattern=r"^pickshop:-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_main, pattern=r"^main$"))
    app.add_handler(CallbackQueryHandler(cb_catalog, pattern=r"^cat$"))
    app.add_handler(CallbackQueryHandler(cb_product, pattern=r"^prod:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_add_to_cart, pattern=r"^add:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_add_kit_to_cart, pattern=r"^addkit:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cart, pattern=r"^cart$"))
    app.add_handler(CallbackQueryHandler(cb_clear_kit_price, pattern=r"^clearkit:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sub_cart, pattern=r"^sub:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_rm_cart, pattern=r"^rm:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_clear_cart, pattern=r"^clearcart$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_order, pattern=r"^cancelord:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_my_orders, pattern=r"^myorders$"))
    app.add_handler(CallbackQueryHandler(cb_view_order, pattern=r"^vieword:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_help, pattern=r"^help$"))

    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^admin$"))
    app.add_handler(CallbackQueryHandler(cb_owner_clear_inv, pattern=r"^owner_clear_inv$"))
    app.add_handler(CallbackQueryHandler(cb_owner_clear_inv_yes, pattern=r"^owner_clear_inv_yes$"))
    app.add_handler(CallbackQueryHandler(cb_adm_prods, pattern=r"^adm_prods$"))
    app.add_handler(CallbackQueryHandler(cb_adm_product, pattern=r"^admp:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_toggle_product, pattern=r"^togglep:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_del_product, pattern=r"^delp:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_view_coa, pattern=r"^viewcoa:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_clear_coa, pattern=r"^clearcoa:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_adm_massedit, pattern=r"^adm_massedit$"))
    app.add_handler(CallbackQueryHandler(cb_mass_download, pattern=r"^mass_dl$"))
    app.add_handler(CallbackQueryHandler(cb_adm_orders, pattern=r"^adm_orders$"))
    app.add_handler(CallbackQueryHandler(cb_adm_awaiting, pattern=r"^adm_awaiting$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reject, pattern=r"^admreject:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_view_proof, pattern=r"^viewproof:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_adm_pays, pattern=r"^adm_pays$"))
    app.add_handler(CallbackQueryHandler(cb_toggle_method, pattern=r"^togglem:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_del_method, pattern=r"^delm:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_adm_ship, pattern=r"^adm_ship$"))
    app.add_handler(CallbackQueryHandler(cb_ship_toggle, pattern=r"^ship_toggle$"))
    app.add_handler(CallbackQueryHandler(cb_adm_minorder, pattern=r"^adm_minorder$"))
    app.add_handler(CallbackQueryHandler(cb_minord_label, pattern=r"^minord_lab:(vial|kit)$"))
    app.add_handler(CallbackQueryHandler(cb_minord_qty, pattern=r"^minord_q:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_adm_admins, pattern=r"^adm_admins$"))
    app.add_handler(CallbackQueryHandler(cb_rm_admin, pattern=r"^rmadmin:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_adm_link, pattern=r"^adm_link$"))
    app.add_handler(CallbackQueryHandler(cb_adm_export, pattern=r"^adm_export$"))
    app.add_handler(CallbackQueryHandler(cb_export_report, pattern=r"^export:(inv|pending|both)$"))

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if err is None:
            return
        log.error("Handler error: %s", err, exc_info=err)
        if TOKEN_FAILOVER and token_pool.is_fatal_token_error(err):
            log.critical(
                "Fatal token error (%s) — stopping for failover",
                type(err).__name__,
            )
            context.application.bot_data["token_dead"] = True
            context.application.bot_data["token_dead_reason"] = str(err)
            try:
                context.application.stop_running()
            except Exception:
                pass

    app.add_error_handler(_on_error)
    return app


def _acquire_single_instance_lock() -> object | None:
    """
    Prevent two local bot.py processes from polling the same token.
    Dual getUpdates causes Telegram 409 Conflict and dropped orders.
    Returns a lock handle that must stay open for the process lifetime.
    """
    import atexit

    lock_path = Path(DB_PATH).resolve().parent / ".bot_polling.lock"
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not open instance lock %s: %s", lock_path, exc)
        return None

    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.seek(0)
                other = (fh.read() or "").strip() or "unknown"
                fh.close()
                print(
                    "ERROR: Another inventory bot is already running "
                    f"(lock held by pid {other}).\n"
                    "Stop the other window/process first — two bots "
                    "cause Telegram Conflict and missed orders.",
                    file=sys.stderr,
                )
                sys.exit(2)
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.seek(0)
                other = (fh.read() or "").strip() or "unknown"
                fh.close()
                print(
                    "ERROR: Another inventory bot is already running "
                    f"(lock held by pid {other}).",
                    file=sys.stderr,
                )
                sys.exit(2)

        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()

        def _release() -> None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
            except Exception:
                pass

        atexit.register(_release)
        log.info("Single-instance lock acquired pid=%s path=%s", os.getpid(), lock_path)
        return fh
    except Exception as exc:
        log.warning("Instance lock failed (continuing): %s", exc)
        try:
            fh.close()
        except Exception:
            pass
        return None


def main() -> None:
    setup_logging()
    # Python 3.12+ / 3.14: ensure a main-thread event loop exists for PTB
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # Keep handle alive for process lifetime
    _instance_lock = _acquire_single_instance_lock()  # noqa: F841

    tokens = resolve_bot_tokens()
    if not tokens:
        print("ERROR: Set TELEGRAM_BOT_TOKEN or BOT_TOKENS in .env")
        sys.exit(1)

    start_idx = token_pool.resolve_active_index(
        tokens, ACTIVE_BOT_INDEX, TOKEN_STATE_PATH
    )
    attempts = len(tokens) if TOKEN_FAILOVER else 1
    idx = start_idx
    tried: set[int] = set()

    for _attempt in range(attempts):
        if idx in tried and len(tried) >= len(tokens):
            break
        tried.add(idx)
        token = tokens[idx]
        fp = token_pool.token_fingerprint(token)
        st = token_pool.load_state(TOKEN_STATE_PATH)
        token_pool.save_state(
            TOKEN_STATE_PATH,
            {
                "active_index": idx,
                "dead_tokens": st.get("dead_tokens") or [],
            },
        )
        log.info(
            "%s starting… token_index=%s fp=%s owners=%s log=%s failover=%s",
            BRAND_NAME,
            idx,
            fp,
            sorted(OWNER_IDS),
            LOG_PATH,
            TOKEN_FAILOVER,
        )
        if BACKUP_PASSPHRASE:
            log.info(
                "Encrypted backups enabled dir=%s retention_days=%s",
                BACKUP_DIR,
                BACKUP_RETENTION_DAYS,
            )
        else:
            log.warning(
                "BACKUP_PASSPHRASE not set — paid-confirm vault snapshots disabled"
            )

        try:
            app = build_app(token)
            app.run_polling(
                allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
            )
            if app.bot_data.get("token_dead"):
                reason = app.bot_data.get("token_dead_reason", "unknown")
                log.critical("Token marked dead during run (%s): %s", fp, reason)
                if not TOKEN_FAILOVER or len(tokens) < 2:
                    raise SystemExit(
                        f"Bot token unusable and no standby: {reason}"
                    )
                idx = token_pool.mark_token_dead(
                    TOKEN_STATE_PATH, token, tokens, idx
                )
                log.warning("Failing over to token index %s", idx)
                continue
            log.info("Polling stopped cleanly for token %s", fp)
            return
        except SystemExit:
            raise
        except Exception as exc:
            # InvalidToken often raised at startup before / during init
            if (
                TOKEN_FAILOVER
                and token_pool.is_fatal_token_error(exc)
                and len(tokens) > 1
            ):
                log.critical("Startup token error (%s): %s", fp, exc)
                idx = token_pool.mark_token_dead(
                    TOKEN_STATE_PATH, token, tokens, idx
                )
                log.warning("Failing over to token index %s", idx)
                continue
            raise

    raise SystemExit("All BOT_TOKENS failed or failover disabled")


if __name__ == "__main__":
    main()
