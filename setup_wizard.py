"""Guided shop setup for group admins (and re-run /setup)."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
import payment_templates as pt
from permissions import can_setup_group_shop, is_group_admin

log = logging.getLogger("inventory_bot.setup")

# Conversation states (offset to avoid collision with main bot states 0-13)
(
    WIZ_NAME,
    WIZ_PAY_DETAILS,
    WIZ_SHIP_FEE,
    WIZ_SHIP_FREE,
    WIZ_PROD_NAME,
    WIZ_PROD_PRICE,
    WIZ_PROD_STOCK,
) = range(100, 107)


def _sid(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    v = context.user_data.get("setup_chat_id")
    return int(v) if v is not None else None


def _pay_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Cash App", callback_data="wizpay:cashapp"),
                InlineKeyboardButton("➕ Venmo", callback_data="wizpay:venmo"),
            ],
            [
                InlineKeyboardButton("➕ Crypto", callback_data="wizpay:crypto"),
                InlineKeyboardButton("➕ Zelle", callback_data="wizpay:zelle"),
            ],
            [
                InlineKeyboardButton("➕ Custom", callback_data="wizpay:custom"),
                InlineKeyboardButton("Done adding", callback_data="wizpay:done"),
            ],
        ]
    )


def _ship_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data="wizship:yes"),
                InlineKeyboardButton("No", callback_data="wizship:no"),
            ],
            [InlineKeyboardButton("Skip shipping", callback_data="wizship:skip")],
        ]
    )


def _product_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add product", callback_data="wizprod:add")],
            [InlineKeyboardButton("Skip — I'll add later", callback_data="wizprod:skip")],
        ]
    )


def _existing_shop_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Edit payments", callback_data="wizexist:pays")],
            [InlineKeyboardButton("📦 Add product", callback_data="wizexist:prod")],
            [InlineKeyboardButton("🔁 Re-run setup", callback_data="wizexist:rerun")],
            [InlineKeyboardButton("Not now", callback_data="wizexist:cancel")],
        ]
    )


async def _safe_reply(update: Update, text: str, **kwargs: Any) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, **kwargs)
        except Exception:
            await update.callback_query.message.reply_text(text, **kwargs)
    elif update.message:
        await update.message.reply_text(text, **kwargs)


async def offer_setup(
    bot: Any,
    chat_id: int,
    chat_title: str,
    user_id: int,
) -> None:
    """DM preferred onboarding offer; group fallback if user hasn't /start'd bot."""
    text = (
        f"Thanks for adding me to *{chat_title or 'this group'}*!\n\n"
        "Want me to set up a shop here? I'll walk you through it in under 2 minutes."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Set up shop", callback_data=f"wizoffer:yes:{chat_id}"
                ),
                InlineKeyboardButton("Not now", callback_data=f"wizoffer:no:{chat_id}"),
            ]
        ]
    )
    try:
        await bot.send_message(
            user_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return
    except Exception as exc:
        log.info("DM setup offer failed user=%s: %s", user_id, exc)

    try:
        me = await bot.get_me()
        deep = f"https://t.me/{me.username}?start=setup_{chat_id}"
        await bot.send_message(
            chat_id,
            f"{text}\n\nOpen me in DM to finish setup:\n{deep}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open setup in DM", url=deep)]]
            ),
        )
    except Exception as exc:
        log.info("Group setup fallback failed chat=%s: %s", chat_id, exc)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When bot is added/promoted in a group, offer setup to the actor."""
    mcm = update.my_chat_member
    if not mcm:
        return
    chat = mcm.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    old = mcm.old_chat_member.status
    new = mcm.new_chat_member.status
    # Bot joined or gained membership
    joined = new in ("member", "administrator") and old in (
        "left",
        "kicked",
        "restricted",
    )
    promoted = new == "administrator" and old == "member"
    if not (joined or promoted):
        return

    actor = mcm.from_user
    if not actor or actor.is_bot:
        return

    allowed = await can_setup_group_shop(context.bot, chat.id, actor.id)
    if not allowed:
        # Still allow if they can manage the group; otherwise quiet
        if not await is_group_admin(context.bot, chat.id, actor.id):
            return

    # Ensure shop row exists (not complete until wizard finishes)
    db.ensure_shop(chat.id, title=chat.title or "Shop")
    if chat.title:
        db.update_shop(chat.id, title=chat.title)

    await offer_setup(context.bot, chat.id, chat.title or "this group", actor.id)


async def cb_wiz_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    # wizoffer:yes:chat_id
    if len(parts) < 3:
        return ConversationHandler.END
    action, chat_s = parts[1], parts[2]
    try:
        chat_id = int(chat_s)
    except ValueError:
        return ConversationHandler.END

    if action == "no":
        await query.edit_message_text("No problem — run /setup in the group anytime.")
        return ConversationHandler.END

    user = update.effective_user
    if not user:
        return ConversationHandler.END

    allowed = await can_setup_group_shop(context.bot, chat_id, user.id)
    if not allowed:
        await query.edit_message_text(
            "You need to be a *group admin* (or bot owner) to set up this shop.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    return await _begin_wizard(update, context, chat_id)


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ /setup in a group or deep-link setup from DM. """
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return ConversationHandler.END

    # Deep link handled in /start: setup_<id> sets user_data before redirect
    if context.user_data.get("setup_chat_id") and chat.type == ChatType.PRIVATE:
        chat_id = int(context.user_data["setup_chat_id"])
        allowed = await can_setup_group_shop(context.bot, chat_id, user.id)
        if not allowed:
            await update.message.reply_text(
                "You need to be a group admin (or bot owner) to set up that shop."
            )
            return ConversationHandler.END
        return await _begin_wizard(update, context, chat_id)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text(
            "Run /setup *inside a group* where I'm a member, "
            "or add me to a group and accept the setup prompt.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    allowed = await can_setup_group_shop(context.bot, chat.id, user.id)
    if not allowed:
        await update.message.reply_text(
            "Only Telegram *group admins* or bot owners can run /setup here.\n"
            "Make sure I'm added to the group with permission to read messages if needed.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    return await _begin_wizard(update, context, chat.id, group_title=chat.title)


async def _begin_wizard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    group_title: str | None = None,
) -> int:
    user = update.effective_user
    shop = db.get_shop(chat_id) or db.ensure_shop(
        chat_id, title=group_title or "Shop"
    )
    if group_title:
        db.update_shop(chat_id, title=group_title)

    context.user_data["setup_chat_id"] = chat_id
    context.user_data.pop("pay_type", None)
    context.user_data.pop("pay_answers", None)
    context.user_data.pop("pay_prompt_i", None)

    if int(shop.get("setup_complete") or 0) == 1:
        text = (
            f"Shop *{shop.get('title') or chat_id}* already exists.\n\n"
            "What would you like to do?"
        )
        await _safe_reply(
            update, text, parse_mode=ParseMode.MARKDOWN, reply_markup=_existing_shop_kb()
        )
        # Stay out of conversation until they pick re-run; use callback-only menu
        return ConversationHandler.END

    db.add_admin(chat_id, user.id, user.username, user.id)
    await _safe_reply(
        update,
        "What should we call this shop? (This is what buyers will see.)",
    )
    return WIZ_NAME


async def cb_existing_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":")[-1]
    chat_id = _sid(context)
    # setup_chat_id may be missing if callback from offer — parse not available
    if chat_id is None and query.message and query.message.chat.type == ChatType.PRIVATE:
        # try from last offer in data not stored — user should /setup in group
        pass

    if action == "cancel":
        await query.edit_message_text("OK — use /setup anytime.")
        return ConversationHandler.END

    if action == "pays":
        # Jump to payment add flow for current shop
        if chat_id is None:
            await query.edit_message_text("Run /setup in the group shop first.")
            return ConversationHandler.END
        context.user_data["setup_chat_id"] = chat_id
        await query.edit_message_text(
            "How do you want to get paid? Add one or more — you can always add more later.",
            reply_markup=_pay_kb(),
        )
        return WIZ_PAY_DETAILS  # wait for wizpay callback; details use same state

    if action == "prod":
        if chat_id is None:
            await query.edit_message_text("Run /setup in the group shop first.")
            return ConversationHandler.END
        await query.edit_message_text(
            "Want to add a product now?",
            reply_markup=_product_kb(),
        )
        return WIZ_PROD_NAME

    if action == "rerun":
        if chat_id is None:
            # Allow rerun only if we know shop
            await query.edit_message_text(
                "Open /setup *inside the group* to re-run setup.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END
        db.update_shop(chat_id, setup_complete=0)
        user = update.effective_user
        db.add_admin(chat_id, user.id, user.username, user.id)
        await query.edit_message_text(
            "What should we call this shop? (This is what buyers will see.)"
        )
        return WIZ_NAME

    return ConversationHandler.END


async def wiz_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = _sid(context)
    if chat_id is None:
        await update.message.reply_text("Setup expired. Run /setup in the group again.")
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Please send a shop name.")
        return WIZ_NAME
    db.update_shop(chat_id, title=name)
    user = update.effective_user
    db.add_admin(chat_id, user.id, user.username, user.id)
    await update.message.reply_text(
        "How do you want to get paid? Add one or more — you can always add more later.",
        reply_markup=_pay_kb(),
    )
    return WIZ_PAY_DETAILS


async def cb_wiz_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mt = (query.data or "").split(":")[-1]
    chat_id = _sid(context)
    if chat_id is None:
        await query.edit_message_text("Setup expired. Run /setup again.")
        return ConversationHandler.END

    if mt == "done":
        await query.edit_message_text(
            "Do you charge shipping?",
            reply_markup=_ship_kb(),
        )
        return WIZ_SHIP_FEE

    if mt not in pt.METHOD_TYPES:
        return WIZ_PAY_DETAILS

    context.user_data["pay_type"] = mt
    context.user_data["pay_answers"] = []
    context.user_data["pay_prompt_i"] = 0
    prompts = pt.template_prompts(mt)
    await query.edit_message_text(prompts[0] + "\n\n/cancel to abort setup.")
    return WIZ_PAY_DETAILS


async def wiz_pay_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect structured payment fields via text."""
    # If this is a callback we shouldn't be here
    if update.callback_query:
        return await cb_wiz_pay(update, context)

    chat_id = _sid(context)
    mt = context.user_data.get("pay_type")
    if chat_id is None or not mt:
        await update.message.reply_text(
            "Pick a payment type:",
            reply_markup=_pay_kb(),
        )
        return WIZ_PAY_DETAILS

    answers: list = context.user_data.setdefault("pay_answers", [])
    prompts = pt.template_prompts(mt)
    answers.append((update.message.text or "").strip())
    context.user_data["pay_answers"] = answers
    i = len(answers)
    if i < len(prompts):
        await update.message.reply_text(prompts[i])
        return WIZ_PAY_DETAILS

    payload = pt.render_from_answers(mt, answers)
    mid = db.add_payment_from_template(chat_id, payload)
    context.user_data.pop("pay_type", None)
    context.user_data.pop("pay_answers", None)
    await update.message.reply_text(
        f"✅ Added *{payload['name']}* (#{mid})\n\n"
        "Add another method or tap *Done adding*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_pay_kb(),
    )
    return WIZ_PAY_DETAILS


async def cb_wiz_ship(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = _sid(context)
    if chat_id is None:
        await query.edit_message_text("Setup expired.")
        return ConversationHandler.END
    action = (query.data or "").split(":")[-1]

    if action in ("no", "skip"):
        db.update_shop(chat_id, shipping_enabled=0 if action == "no" else 1)
        await query.edit_message_text(
            "Want to add your first product now?",
            reply_markup=_product_kb(),
        )
        return WIZ_PROD_NAME

    # yes — ask fee
    await query.edit_message_text(
        "What's the flat shipping fee? (number, e.g. `8`)\n"
        "Or send `skip` to use defaults.",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["ship_step"] = "fee"
    return WIZ_SHIP_FEE


async def wiz_ship_fee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = _sid(context)
    if chat_id is None:
        return ConversationHandler.END
    raw = (update.message.text or "").strip().lower()
    if raw == "skip":
        db.update_shop(chat_id, shipping_enabled=1)
        await update.message.reply_text(
            "Want to add your first product now?",
            reply_markup=_product_kb(),
        )
        return WIZ_PROD_NAME
    try:
        fee = float(raw.replace("$", ""))
        if fee < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a number (e.g. 8) or `skip`.")
        return WIZ_SHIP_FEE
    context.user_data["ship_fee"] = fee
    await update.message.reply_text(
        "Free shipping over what amount? (e.g. `150`)\n"
        "Or send `skip` for no free-shipping threshold."
    )
    return WIZ_SHIP_FREE


async def wiz_ship_free(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = _sid(context)
    if chat_id is None:
        return ConversationHandler.END
    raw = (update.message.text or "").strip().lower()
    fee = float(context.user_data.get("ship_fee") or 8)
    free_above = 0.0
    if raw != "skip":
        try:
            free_above = float(raw.replace("$", ""))
            if free_above < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Send a number or `skip`.")
            return WIZ_SHIP_FREE
    db.update_shop(
        chat_id,
        shipping_enabled=1,
        shipping_fee=fee,
        free_shipping_above=free_above,
    )
    await update.message.reply_text(
        "Want to add your first product now?",
        reply_markup=_product_kb(),
    )
    return WIZ_PROD_NAME


async def cb_wiz_prod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":")[-1]
    if action == "skip":
        return await _finish_wizard(update, context)
    await query.edit_message_text("Product name?")
    return WIZ_PROD_NAME


async def wiz_prod_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Could be text after "Add product" or skip already handled
    if update.callback_query:
        return await cb_wiz_prod(update, context)
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Send a product name.")
        return WIZ_PROD_NAME
    context.user_data["prod_name"] = name
    await update.message.reply_text("Price? (number, e.g. `50`)")
    return WIZ_PROD_PRICE


async def wiz_prod_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip().replace("$", "")
    try:
        price = float(raw)
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a valid price number.")
        return WIZ_PROD_PRICE
    context.user_data["prod_price"] = price
    await update.message.reply_text("Starting stock quantity? (integer, e.g. `10`)")
    return WIZ_PROD_STOCK


async def wiz_prod_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = _sid(context)
    if chat_id is None:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    try:
        stock = int(raw)
        if stock < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a whole number for stock.")
        return WIZ_PROD_STOCK
    name = context.user_data.get("prod_name") or "Product"
    price = float(context.user_data.get("prod_price") or 0)
    pid = db.add_product(chat_id, name=name, price=price, stock=stock)
    await update.message.reply_text(f"✅ Added product *{name}* (#{pid})", parse_mode=ParseMode.MARKDOWN)
    return await _finish_wizard(update, context)


async def _finish_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = _sid(context)
    user = update.effective_user
    if chat_id is None:
        return ConversationHandler.END
    db.update_shop(chat_id, setup_complete=1)
    if user:
        db.add_admin(chat_id, user.id, user.username, user.id)

    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=shop_{chat_id}"
    shop = db.get_shop(chat_id) or {}
    text = (
        f"✅ *Shop's live!* — *{shop.get('title') or 'Shop'}*\n\n"
        f"Buyers can start shopping here:\n`{link}`\n\n"
        "Manage products, payments, and orders anytime with /admin\n"
        "Edit this setup anytime with /setup\n\n"
        "_Stock only drops after you confirm a buyer's payment._"
    )
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            await context.bot.send_message(
                update.effective_user.id, text, parse_mode=ParseMode.MARKDOWN
            )
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("setup_chat_id", None)
    context.user_data.pop("pay_type", None)
    if update.message:
        await update.message.reply_text("Setup cancelled. Run /setup anytime.")
    return ConversationHandler.END


def build_setup_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("setup", cmd_setup),
            CallbackQueryHandler(cb_wiz_offer, pattern=r"^wizoffer:(yes|no):-?\d+$"),
            CallbackQueryHandler(cb_existing_menu, pattern=r"^wizexist:"),
        ],
        states={
            WIZ_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_name),
            ],
            WIZ_PAY_DETAILS: [
                CallbackQueryHandler(cb_wiz_pay, pattern=r"^wizpay:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_pay_details),
            ],
            WIZ_SHIP_FEE: [
                CallbackQueryHandler(cb_wiz_ship, pattern=r"^wizship:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_ship_fee),
            ],
            WIZ_SHIP_FREE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_ship_free),
            ],
            WIZ_PROD_NAME: [
                CallbackQueryHandler(cb_wiz_prod, pattern=r"^wizprod:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_prod_name),
            ],
            WIZ_PROD_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_prod_price),
            ],
            WIZ_PROD_STOCK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_prod_stock),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_setup),
            CallbackQueryHandler(cb_wiz_ship, pattern=r"^wizship:"),
            CallbackQueryHandler(cb_wiz_prod, pattern=r"^wizprod:"),
        ],
        name="setup_wizard",
        allow_reentry=True,
        persistent=False,
    )
