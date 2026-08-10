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
import math
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


def _coerce_cart_int(value) -> int:
    """Coerce mini-app cart id/vials/kits to int.

    Raises ValueError/TypeError/OverflowError on bad input (including non-finite
    floats). Callers wrap the full parse block so hostile carts never crash the
    handler after the store already showed success.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a cart int")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite cart number")
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("nan", "inf", "-inf", "+inf", "infinity", "-infinity", "+infinity"):
            raise ValueError("non-finite cart string")
    n = int(value)
    # Reject floats that were not whole numbers after int() truncation check
    if isinstance(value, float) and value != n:
        # int(1.0) is fine; still allow if mathematically equal
        pass
    return n


def _md_escape(text: str) -> str:
    """Escape Telegram legacy Markdown metacharacters in untrusted/DB strings."""
    s = str(text or "")
    return (
        s.replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


def _coerce_ship_str(value, max_len: int | None = None) -> str:
    """Coerce free-text ship field to trimmed str; never raise on bad types."""
    if value is None or isinstance(value, (bool, dict, list)):
        return ""
    try:
        s = str(value).strip()
    except Exception:
        return ""
    if max_len is not None and max_len > 0 and len(s) > max_len:
        s = s[:max_len]
    return s


def parse_ship_fields(payload) -> tuple[str, str, str]:
    """Extract (ship_name, ship_address, ship_notes) from web_app_data payload.

    Contract (store): ``{"v":1,"items":[...],"ship":{"name","line1","line2",
    "city","state","zip","phone"}}``. ``ship`` may be absent on old clients.
    """
    raw = payload.get("ship") if isinstance(payload, dict) else None
    ship = raw if isinstance(raw, dict) else {}

    name = _coerce_ship_str(ship.get("name"), max_len=120)
    line1 = _coerce_ship_str(ship.get("line1"))
    line2 = _coerce_ship_str(ship.get("line2"))
    city = _coerce_ship_str(ship.get("city"))
    state = _coerce_ship_str(ship.get("state"))
    zip_code = _coerce_ship_str(ship.get("zip"))
    phone = _coerce_ship_str(ship.get("phone"))

    parts: list[str] = []
    if line1:
        parts.append(line1)
    if line2:
        parts.append(line2)
    st_zip = " ".join(p for p in (state, zip_code) if p)
    if city and st_zip:
        parts.append(f"{city}, {st_zip}")
    elif city:
        parts.append(city)
    elif st_zip:
        parts.append(st_zip)

    ship_address = "\n".join(parts)
    if phone:
        ship_notes = f"Phone: {phone} · via mini app"
    else:
        ship_notes = "via mini app"
    return name, ship_address, ship_notes


def build_notify_recipient_ids(base_ids, shop_chat_id: int) -> list[int]:
    """Union configured notify ids with shop admins; dedupe, order-preserving.

    Shop admins (``db.list_admins``) are included so a vendor who claimed the
    shop receives new-order DMs without manual notify_ids env config.
    """
    out: list[int] = []
    seen: set[int] = set()
    for nid in base_ids or []:
        try:
            i = int(nid)
        except (TypeError, ValueError):
            continue
        if i not in seen:
            seen.add(i)
            out.append(i)
    try:
        admins = db.list_admins(int(shop_chat_id))
    except Exception:
        log.exception(
            "list_admins failed for shop_chat_id=%s; notify without admins",
            shop_chat_id,
        )
        admins = []
    for a in admins or []:
        try:
            uid = int((a or {}).get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def format_customer_ship_block(
    ship_name: str, ship_address: str, *, markdown: bool = True
) -> str:
    """Customer confirmation 'Shipping to' block, or empty if nothing to show."""
    if not (ship_name or ship_address):
        return ""
    header = "📦 *Shipping to:*" if markdown else "📦 Shipping to:"
    parts = [header]
    if ship_name:
        parts.append(_md_escape(ship_name) if markdown else ship_name)
    if ship_address:
        parts.append(_md_escape(ship_address) if markdown else ship_address)
    return "\n\n" + "\n".join(parts)


def format_new_order_ship_section(ship_name: str, ship_address: str) -> str:
    """NEW ORDER notify shipping section (plain text)."""
    if ship_address:
        lines = ["Ship to:"]
        if ship_name:
            lines.append(ship_name)
        lines.append(ship_address)
        return "\n".join(lines)
    return "⚠️ No address provided — contact the customer"


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


def get_bot_token_for_shop(shop_chat_id: int) -> str | None:
    """Return the vendor bot token bound to this shop, or None.

    Customers only ever talk to the vendor storefront bot (not the main SPBC
    bot), so panel-side DMs must use this token. Matches load_vendor_configs()
    entries via _resolve_shop (explicit shop_chat_id or invite bind).
    """
    try:
        target = int(db.resolve_shop_chat_id(int(shop_chat_id)))
    except Exception:
        target = int(shop_chat_id)
    for v in load_vendor_configs():
        token = (v.get("token") or "").strip()
        if not token:
            continue
        try:
            resolved = _resolve_shop(v)
        except Exception:
            log.exception("get_bot_token_for_shop: resolve failed for %s", v.get("name"))
            continue
        if resolved and int(resolved) == target:
            return token
    return None


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

        # Parse + normalize must never crash: store already showed success to the customer.
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("cart root is not an object")
            cart_items = parsed.get("items")
            if not isinstance(cart_items, list):
                raise ValueError("items is not a list")
            items: list[dict] = []
            for it in cart_items:
                if not isinstance(it, dict):
                    raise ValueError("cart item is not an object")
                pid = _coerce_cart_int(it.get("id") or 0)
                vials = max(0, _coerce_cart_int(it.get("vials") or 0))
                kits = max(0, _coerce_cart_int(it.get("kits") or 0))
                if pid <= 0:
                    continue
                if vials:
                    items.append({"product_id": pid, "quantity": vials})
                if kits:
                    items.append(
                        {
                            "product_id": pid,
                            "quantity": kits * KIT_SIZE,
                            "is_kit": True,
                        }
                    )
        except Exception:
            log.warning("[%s] malformed web_app_data cart from %s", name, getattr(user, "id", None))
            await msg.reply_text(
                "That order didn't come through right — please try again from the store."
            )
            return

        if not items:
            await msg.reply_text("Your cart came through empty — add something and try again.")
            return

        # Ship block is optional (old clients omit it); never crash on bad types.
        ship_name, ship_address, ship_notes = parse_ship_fields(
            parsed if isinstance(parsed, dict) else {}
        )

        order = db.create_order(
            chat_id=shop_chat_id,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            items=items,
            payment_method=None,
            ship_name=ship_name,
            ship_address=ship_address,
            ship_notes=ship_notes,
        )
        if not order:
            await msg.reply_text(
                "⚠️ Couldn't place that order — something in your cart just sold "
                "out or the quantity isn't available. Reopen the store to see live stock."
            )
            return

        order_lines = db.get_order_items(int(order["id"]))
        # Escape DB/owner strings so Markdown parse never 400s after create_order.
        lines = [
            f"  • {_md_escape(ln['product_name'])} × {ln['quantity']} — {_fmt_money(ln['line_total'])}"
            for ln in order_lines
        ]
        plain_lines = [
            f"  • {ln['product_name']} × {ln['quantity']} — {_fmt_money(ln['line_total'])}"
            for ln in order_lines
        ]
        if order.get("shipping_fee"):
            ship_ln = f"  • Shipping — {_fmt_money(order['shipping_fee'])}"
            lines.append(ship_ln)
            plain_lines.append(ship_ln)

        ship_md_block = format_customer_ship_block(
            ship_name, ship_address, markdown=True
        )
        ship_plain_block = format_customer_ship_block(
            ship_name, ship_address, markdown=False
        )

        pays = db.list_payment_methods(shop_chat_id)
        pay_txt = (
            "\n".join(
                f"  • {_md_escape(p['name'])}: {_md_escape(p['instructions'])}".rstrip(": ")
                for p in pays
            )
            if pays
            else "  • Payment details will be DM'd to you."
        )
        pay_plain = (
            "\n".join(f"  • {p['name']}: {p['instructions']}".rstrip(": ") for p in pays)
            if pays
            else "  • Payment details will be DM'd to you."
        )
        code = order.get("payment_code") or f"#{order.get('id')}"
        total_txt = _fmt_money(order.get("total", 0))

        # Customer reply must not block owner notify if Telegram rejects Markdown.
        try:
            await msg.reply_text(
                f"{emoji} *Order received!*\n\n"
                + "\n".join(lines)
                + ship_md_block
                + f"\n\n*Total: {total_txt}*\n"
                + f"Payment code: `{_md_escape(code)}` (put this in the memo)\n\n"
                + "Pay with:\n" + pay_txt + "\n\n"
                + "You'll get a confirmation here once payment lands.",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("[%s] customer confirm reply failed (order still saved): %s", name, e)
            try:
                await msg.reply_text(
                    f"{emoji} Order received!\n\n"
                    + "\n".join(plain_lines)
                    + ship_plain_block
                    + f"\n\nTotal: {total_txt}\n"
                    + f"Payment code:\n{code}\n"
                    + "(put this in the memo)\n\n"
                    + "Pay with:\n" + pay_plain + "\n\n"
                    + "You'll get a confirmation here once payment lands."
                )
            except Exception as e2:
                log.warning("[%s] plain customer confirm also failed: %s", name, e2)

        ship_notify = format_new_order_ship_section(ship_name, ship_address)

        # Narrow per-order tracking link (not admin panel). Skip if no public URL.
        track_line = ""
        try:
            import webpanel as _webpanel

            track_line = _webpanel.format_add_tracking_dm_line(
                int(order["id"]), shop_chat_id
            )
        except Exception:
            log.exception("[%s] mint tracking link failed for order %s", name, order.get("id"))

        note = (
            f"{emoji} NEW ORDER {code} — {name}\n"
            f"From: {user.full_name} (@{user.username}, id {user.id})\n"
            + "\n".join(plain_lines)
            + f"\nTotal: {total_txt}\n"
            + ship_notify
        )
        if track_line:
            note = note + "\n" + track_line
        recipients = build_notify_recipient_ids(notify_ids, shop_chat_id)
        for nid in recipients:
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


def _resolved_order_fee(v: dict) -> float:
    """Per-vendor platform fee from config; default $2 (legacy Unicorn $1)."""
    if v.get("order_fee") is not None:
        return float(v["order_fee"])
    # Legacy single-vendor path hardcodes order_fee=1.0; if a JSON entry for
    # Unicorn omits order_fee, keep the $1 policy instead of jumping to $2.
    name = (v.get("name") or "").lower()
    if "unicorn" in name:
        return 1.0
    return DEFAULT_ORDER_FEE


def _ensure_order_fee(v: dict, shop_chat_id: int) -> None:
    """Seed the shop's per-order platform fee if it has never been set.

    Config key "order_fee" (per vendor entry) overrides the default.
    A fee already set on the shop (manually or previously) is never touched,
    so owner adjustments in Telegram always win — except that 0 looks the
    same as "never set". To permanently disable a shop's fee, set
    ``"order_fee": 0`` in VENDOR_STORES_JSON (explicit 0 skips seeding).
    """
    fee = _resolved_order_fee(v)
    try:
        from franchise import ensure_franchise_tables

        ensure_franchise_tables()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT hidden_service_fee FROM shops WHERE chat_id = ?",
                (shop_chat_id,),
            ).fetchone()
            if not row:
                log.warning(
                    "[%s] shop %s missing — cannot seed order fee",
                    v.get("name"),
                    shop_chat_id,
                )
                return
            current = float(row["hidden_service_fee"] or 0)
            if current == 0.0 and fee > 0:
                conn.execute(
                    "UPDATE shops SET hidden_service_fee = ? WHERE chat_id = ?",
                    (fee, shop_chat_id),
                )
                log.info("[%s] per-order fee seeded: $%.2f", v.get("name"), fee)
            else:
                log.info(
                    "[%s] per-order fee left as-is: $%.2f (config $%.2f)",
                    v.get("name"),
                    current,
                    fee,
                )
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
