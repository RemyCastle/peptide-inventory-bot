r"""
Multi-vendor mini-app order receivers for the SPBC inventory bot.

Each vendor gets their own branded Telegram bot (made in BotFather) whose mini
app storefront posts carts back via web_app_data. This module runs ONE polling
thread per configured vendor inside the same process as the main bot, sharing
inventory.db — so every order lands in the same orders tables and the owner
sees a single stream.

Configuration (env):

  VENDOR_STORES_JSON   JSON array, one object per vendor:
      [
        {
          "name":       "Unicorn Magic Factory",     # customer-facing name
          "emoji":      "🦄",                        # flavor for buttons/DMs
          "token":      "123456:ABC...",             # BotFather token
          "invite":     "vendor3a9eee77166edc...",   # handoff-link token -> shop
          "shop_chat_id": -5551234567,               # optional, overrides invite
          "store_url":  "https://.../unicorn/",      # mini app URL
          "notify_ids": [111111111],                 # extra chat ids to DM
          "welcome":    "optional custom /start text"
        }
      ]

  Legacy single-vendor vars (UNICORN_BOT_TOKEN, UNICORN_CLAIM_TOKEN,
  UNICORN_SHOP_CHAT_ID, UNICORN_STORE_URL, UNICORN_NOTIFY_IDS) are still
  honored and merged in, so the first vendor keeps working unchanged.

  OWNER_TELEGRAM_CHAT_ID is always added to every vendor's notify list.

Adding vendor N+1 = create their bot in BotFather, add one JSON entry, done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import KIT_SIZE

log = logging.getLogger("vendor_stores")


# ── configuration ────────────────────────────────────────────────────────────

def _owner_ids() -> list[int]:
    raw = (os.getenv("OWNER_TELEGRAM_CHAT_ID") or "").strip()
    return [int(raw)] if raw.lstrip("-").isdigit() else []


def load_vendor_configs() -> list[dict]:
    vendors: list[dict] = []

    raw = (os.getenv("VENDOR_STORES_JSON") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                vendors.extend(v for v in data if isinstance(v, dict))
        except Exception:
            log.exception("VENDOR_STORES_JSON is not valid JSON - ignoring it")

    # Legacy single-vendor env (first vendor: Unicorn Magic Factory)
    if (os.getenv("UNICORN_BOT_TOKEN") or "").strip():
        legacy_notify = [
            int(x) for x in (os.getenv("UNICORN_NOTIFY_IDS") or "").split(",")
            if x.strip().lstrip("-").isdigit()
        ]
        vendors.append({
            "name": "Unicorn Magic Factory",
            "emoji": "\U0001f984",
            "token": os.getenv("UNICORN_BOT_TOKEN", "").strip(),
            "invite": (os.getenv("UNICORN_CLAIM_TOKEN") or "").strip(),
            "shop_chat_id": (os.getenv("UNICORN_SHOP_CHAT_ID") or "").strip(),
            "store_url": (os.getenv("UNICORN_STORE_URL")
                          or "https://remy-miniapp-demos.pages.dev/unicorn/").strip(),
            "notify_ids": legacy_notify,
            "order_fee": 1.0,  # unicornfartzz pays $1/order; other vendors default $2
        })

    # de-dupe by token (JSON entry wins over legacy)
    seen: set[str] = set()
    unique = []
    for v in vendors:
        tok = (v.get("token") or "").strip()
        if not tok or tok in seen:
            continue
        seen.add(tok)
        unique.append(v)
    return unique


def _parse_chat_id(raw) -> int:
    s = str(raw or "").strip().strip("\"'")
    if not s:
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _resolve_shop(v: dict) -> int:
    name = v.get("name") or "vendor"
    explicit = _parse_chat_id(v.get("shop_chat_id"))
    if explicit:
        resolved = int(db.resolve_shop_chat_id(explicit))
        log.info("[%s] shop from shop_chat_id env/config → %s", name, resolved)
        return resolved
    raw = str(v.get("invite") or "").strip()
    if raw:
        import webpanel

        token = webpanel.normalize_invite_token(raw)
        webpanel.ensure_webpanel_tables()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT shop_chat_id FROM vendor_invites WHERE token_hash = ?",
                (webpanel._hash(token),),
            ).fetchone()
        if row and row["shop_chat_id"]:
            resolved = int(db.resolve_shop_chat_id(int(row["shop_chat_id"])))
            log.info("[%s] shop from invite → %s", name, resolved)
            return resolved
        log.warning(
            "[%s] invite present but no vendor_invites.shop_chat_id (token body len=%s)",
            name,
            len(token),
        )
    else:
        log.warning("[%s] no shop_chat_id and no invite configured", name)
    return 0


# ── per-vendor bot ───────────────────────────────────────────────────────────

def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def _build_app(v: dict, shop_chat_id: int) -> Application:
    name = v.get("name") or "the shop"
    emoji = v.get("emoji") or "\U0001f6cd"
    store_url = v.get("store_url") or ""
    notify_ids = list(dict.fromkeys([*(v.get("notify_ids") or []), *_owner_ids()]))
    welcome = v.get("welcome") or (
        f"{emoji} Welcome to *{name}* {emoji}\n\n"
        "Tap the button below to open the store and send your order straight "
        "back here. Research use only · 21+."
    )

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(f"{emoji} Open the Store", web_app=WebAppInfo(url=store_url))]],
        resize_keyboard=True,
        is_persistent=True,
    )

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_markdown(welcome, reply_markup=keyboard)

    async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"Your chat id: {update.effective_chat.id}")

    async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        raw = msg.web_app_data.data if msg and msg.web_app_data else ""
        log.info("[%s] web_app_data from %s (%s): %s", name, user.id, user.username, raw[:500])

        try:
            cart_items = (json.loads(raw).get("items")) or []
        except Exception:
            await msg.reply_text("That order didn't come through right — please try again from the store.")
            return

        items: list[dict] = []
        for it in cart_items:
            pid = int(it.get("id") or 0)
            vials = max(0, int(it.get("vials") or 0))
            kits = max(0, int(it.get("kits") or 0))
            if pid <= 0:
                continue
            if vials:
                items.append({"product_id": pid, "quantity": vials})
            if kits:
                items.append({"product_id": pid, "quantity": kits * KIT_SIZE, "is_kit": True})

        if not items:
            await msg.reply_text("Your cart came through empty — add something and try again.")
            return

        order = db.create_order(
            chat_id=shop_chat_id,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            items=items,
            payment_method=None,
            ship_name="",
            ship_address="",
            ship_notes="via mini app",
        )
        if not order:
            await msg.reply_text(
                "⚠️ Couldn't place that order — something in your cart just sold "
                "out or the quantity isn't available. Reopen the store to see live stock."
            )
            return

        order_lines = db.get_order_items(int(order["id"]))
        lines = [
            f"  • {ln['product_name']} × {ln['quantity']} — {_fmt_money(ln['line_total'])}"
            for ln in order_lines
        ]
        if order.get("shipping_fee"):
            lines.append(f"  • Shipping — {_fmt_money(order['shipping_fee'])}")
        pays = db.list_payment_methods(shop_chat_id)
        pay_txt = (
            "\n".join(f"  • {p['name']}: {p['instructions']}".rstrip(": ") for p in pays)
            if pays else "  • Payment details will be DM'd to you."
        )
        code = order.get("payment_code") or f"#{order.get('id')}"

        await msg.reply_text(
            f"{emoji} *Order received!*\n\n"
            + "\n".join(lines)
            + f"\n\n*Total: {_fmt_money(order.get('total', 0))}*\n"
            + f"Payment code: `{code}` (put this in the memo)\n\n"
            + "Pay with:\n" + pay_txt + "\n\n"
            + "You'll get a confirmation here once payment lands.",
            parse_mode="Markdown",
        )

        note = (
            f"{emoji} NEW ORDER {code} — {name}\n"
            f"From: {user.full_name} (@{user.username}, id {user.id})\n"
            + "\n".join(lines)
            + f"\nTotal: {_fmt_money(order.get('total', 0))}"
        )
        for nid in notify_ids:
            try:
                await context.bot.send_message(nid, note)
            except Exception as e:
                log.warning("[%s] notify %s failed: %s", name, nid, e)

    app = Application.builder().token(v["token"]).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    return app


DEFAULT_ORDER_FEE = 2.0  # per-order platform fee folded into customer shipping


def _ensure_order_fee(v: dict, shop_chat_id: int) -> None:
    """Seed the shop's per-order platform fee if it has never been set.

    Config key "order_fee" (per vendor entry) overrides DEFAULT_ORDER_FEE.
    A fee already set on the shop (manually or previously) is never touched,
    so owner adjustments in Telegram always win.
    """
    fee = v.get("order_fee")
    fee = DEFAULT_ORDER_FEE if fee is None else float(fee)
    try:
        from franchise import ensure_franchise_tables

        ensure_franchise_tables()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT hidden_service_fee FROM shops WHERE chat_id = ?",
                (shop_chat_id,),
            ).fetchone()
            current = float(row["hidden_service_fee"] or 0) if row else 0.0
            if row and current == 0.0 and fee > 0:
                conn.execute(
                    "UPDATE shops SET hidden_service_fee = ? WHERE chat_id = ?",
                    (fee, shop_chat_id),
                )
                log.info("[%s] per-order fee seeded: $%.2f", v.get("name"), fee)
            else:
                log.info("[%s] per-order fee already set: $%.2f", v.get("name"), current)
    except Exception:
        log.exception("[%s] could not seed order fee", v.get("name"))


def _run_vendor(v: dict) -> None:
    name = v.get("name") or "vendor"
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
        shop_chat_id = _resolve_shop(v)
        if not shop_chat_id:
            log.error("[%s] could not resolve shop (invite/shop_chat_id) - not starting", name)
            return
        _ensure_order_fee(v, shop_chat_id)
        app = _build_app(v, shop_chat_id)
        log.info("[%s] store receiver polling (shop %s)", name, shop_chat_id)
        app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)
    except Exception:
        log.exception("[%s] store receiver crashed", name)


def start_all() -> int:
    """Spawn one daemon thread per configured vendor. Returns vendor count."""
    vendors = load_vendor_configs()
    for v in vendors:
        threading.Thread(
            target=_run_vendor, args=(v,),
            name=f"store-{(v.get('name') or 'vendor')[:16]}", daemon=True,
        ).start()
    if vendors:
        log.info("started %d vendor store receiver(s)", len(vendors))
    return len(vendors)


if __name__ == "__main__":
    # Standalone/local test mode: run all configured vendors in the foreground.
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    n = start_all()
    if not n:
        raise SystemExit("no vendors configured (VENDOR_STORES_JSON or UNICORN_* env)")
    threading.Event().wait()
