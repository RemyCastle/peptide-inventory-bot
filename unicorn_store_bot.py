r"""
Unicorn Magic Factory — mini-app order receiver (@UnicornMagicFactoryBot).

Runs as its OWN bot process next to the main inventory bot, sharing db.py and
inventory.db. It does three things:

  1. /start           -> welcome + a reply-keyboard web_app button that opens the
                         store (sendData only works from keyboard-launched apps).
  2. web_app_data     -> parse the store's cart JSON, re-validate against the DB
                         (prices/stock come from the DB, never from the client),
                         create the order via db.create_order, confirm to the
                         customer, and DM every id in UNICORN_NOTIFY_IDS.
  3. /myid            -> print the sender's chat id (for filling UNICORN_NOTIFY_IDS).

.env keys (in this folder's .env):
  UNICORN_BOT_TOKEN     bot token from BotFather   (required)
  UNICORN_SHOP_CHAT_ID  shops.chat_id of her shop  (required)
  UNICORN_NOTIFY_IDS    comma-separated chat ids to DM on new orders
  UNICORN_STORE_URL     the mini app URL

Run:  venv\Scripts\python.exe unicorn_store_bot.py
"""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import Config

load_dotenv()

TOKEN = (os.getenv("UNICORN_BOT_TOKEN") or "").strip()
STORE_URL = (os.getenv("UNICORN_STORE_URL") or "https://remy-miniapp-demos.pages.dev/unicorn/").strip()
KIT_SIZE = int(getattr(Config, "KIT_SIZE", 10))

NOTIFY_IDS = [int(x) for x in (os.getenv("UNICORN_NOTIFY_IDS") or "").split(",") if x.strip()]
if not NOTIFY_IDS:
    _owner = (os.getenv("OWNER_TELEGRAM_CHAT_ID") or "").strip()
    if _owner.lstrip("-").isdigit():
        NOTIFY_IDS = [int(_owner)]


def _resolve_shop_chat_id() -> int:
    """Shop id from UNICORN_SHOP_CHAT_ID, or resolved from the vendor invite
    token in the handoff link (UNICORN_CLAIM_TOKEN=vendor<hex24> or <hex24>)."""
    explicit = (os.getenv("UNICORN_SHOP_CHAT_ID") or "").strip()
    if explicit.lstrip("-").isdigit():
        return int(explicit)
    raw = (os.getenv("UNICORN_CLAIM_TOKEN") or "").strip()
    if raw.startswith("vendor"):
        raw = raw[len("vendor"):]
    if raw:
        import webpanel

        webpanel.ensure_webpanel_tables()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT shop_chat_id FROM vendor_invites WHERE token_hash = ?",
                (webpanel._hash(raw),),
            ).fetchone()
        if row and row["shop_chat_id"]:
            return int(db.resolve_shop_chat_id(int(row["shop_chat_id"])))
    return 0


SHOP_CHAT_ID = 0  # resolved at startup (needs DB_PATH env to be in effect)

log = logging.getLogger("unicorn_store")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

WELCOME = (
    "✨ Welcome to *Unicorn Magic Factory* ✨\n\n"
    "Tap the button below to open the store, browse the shelf, and roll your "
    "order straight back to us. Research use only · 21+."
)


def store_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("\U0001f984 Open the Factory", web_app=WebAppInfo(url=STORE_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(WELCOME, reply_markup=store_keyboard())


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your chat id: {update.effective_chat.id}")


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    raw = msg.web_app_data.data if msg and msg.web_app_data else ""
    log.info("web_app_data from %s (%s): %s", user.id, user.username, raw[:500])

    try:
        payload = json.loads(raw)
        cart_items = payload.get("items") or []
    except Exception:
        await msg.reply_text("That order didn't come through right — please try again from the store.")
        return

    # Build create_order items. The client sends product ids + quantities only;
    # every price is resolved server-side by db.create_order.
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
        await msg.reply_text("Your cart came through empty — add something sparkly and try again. \U0001f984")
        return

    order = db.create_order(
        chat_id=SHOP_CHAT_ID,
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
            "⚠️ Couldn't place that order — something in your cart just sold out "
            "or the quantity isn't available. Reopen the store to see live stock."
        )
        return

    order_lines = db.get_order_items(int(order["id"]))
    lines = [
        f"  • {ln['product_name']} × {ln['quantity']} — {_fmt_money(ln['line_total'])}"
        for ln in order_lines
    ]
    if order.get("shipping_fee"):
        lines.append(f"  • Shipping — {_fmt_money(order['shipping_fee'])}")
    pays = db.list_payment_methods(SHOP_CHAT_ID)
    pay_txt = (
        "\n".join(f"  • {p['name']}: {p['instructions']}".rstrip(": ") for p in pays)
        if pays else "  • The factory will DM you payment details."
    )
    code = order.get("payment_code") or f"#{order.get('id')}"

    await msg.reply_text(
        "✨ *Order received!* ✨\n\n"
        + "\n".join(lines)
        + f"\n\n*Total: {_fmt_money(order.get('total', 0))}*\n"
        + f"Payment code: `{code}` (put this in the memo)\n\n"
        + "Pay with:\n" + pay_txt + "\n\n"
        + "You'll get a confirmation here once payment lands. \U0001f984",
        parse_mode="Markdown",
    )

    note = (
        f"\U0001f984 NEW ORDER {code}\n"
        f"From: {user.full_name} (@{user.username}, id {user.id})\n"
        + "\n".join(lines)
        + f"\nTotal: {_fmt_money(order.get('total', 0))}"
    )
    for nid in NOTIFY_IDS:
        try:
            await context.bot.send_message(nid, note)
        except Exception as e:  # vendor hasn't /start-ed the bot yet, etc.
            log.warning("notify %s failed: %s", nid, e)


def _build_app() -> Application:
    global SHOP_CHAT_ID
    if not TOKEN:
        raise SystemExit("UNICORN_BOT_TOKEN is empty - paste the BotFather token into .env")
    SHOP_CHAT_ID = _resolve_shop_chat_id()
    if not SHOP_CHAT_ID:
        raise SystemExit(
            "Could not resolve her shop - set UNICORN_SHOP_CHAT_ID or UNICORN_CLAIM_TOKEN"
        )
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    log.info("Unicorn store receiver polling (shop chat_id=%s, notify=%s)", SHOP_CHAT_ID, NOTIFY_IDS)
    return app


def main() -> None:
    _build_app().run_polling(allowed_updates=Update.ALL_TYPES)


def run_threaded() -> None:
    """Entry for run_cloud.py: run inside a daemon thread (no signal handlers)."""
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        _build_app().run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)
    except SystemExit as e:
        log.warning("unicorn store receiver not started: %s", e)
    except Exception:
        log.exception("unicorn store receiver crashed")


if __name__ == "__main__":
    main()
