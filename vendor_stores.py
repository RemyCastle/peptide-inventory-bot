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
import hashlib
import hmac
import html as html_mod
import io
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import KIT_SIZE

log = logging.getLogger("vendor_stores")

# Telegram WebApp initData max age (replay guard)
INIT_DATA_MAX_AGE_SEC = 24 * 60 * 60


class InitDataError(Exception):
    """Telegram WebApp initData failed validation (auth boundary)."""


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


def parse_miniapp_cart_items(cart_items) -> list[dict]:
    """Parse store cart items into create_order line dicts.

    Same rules as on_web_app_data: coerce id/vials/kits via hardened helpers;
    vials → quantity line; kits → kits*KIT_SIZE with is_kit. Raises ValueError
    on structural problems (caller maps to 400).
    """
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
    return items


def validate_webapp_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_sec: int = INIT_DATA_MAX_AGE_SEC,
) -> dict:
    """Validate Telegram.WebApp.initData against the vendor bot token.

    Algorithm (constant-time hash compare + 24h auth_date replay guard):
      - Parse initData as query string; pull out hash
      - data_check_string = sorted key=value pairs excluding hash, joined by \\n
      - secret_key = HMAC_SHA256(key=b\"WebAppData\", msg=bot_token)
      - expected = HMAC_SHA256(key=secret_key, msg=data_check_string).hex()
      - reject unless compare_digest(expected, provided_hash)
      - reject if auth_date older than max_age_sec

    Returns buyer dict: user_id, username, full_name, auth_date.
    Raises InitDataError on any failure.
    """
    raw = (init_data or "").strip()
    token = (bot_token or "").strip()
    if not raw or not token:
        raise InitDataError("missing initData or bot token")

    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    if not pairs:
        raise InitDataError("empty initData")

    provided_hash = ""
    for k, v in pairs:
        if k == "hash":
            provided_hash = v
            break
    if not provided_hash:
        raise InitDataError("missing hash")

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(pairs, key=lambda kv: kv[0]) if k != "hash"
    )
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    try:
        ok = hmac.compare_digest(expected, provided_hash)
    except Exception:
        ok = False
    if not ok:
        raise InitDataError("bad hash")

    # Rebuild map for field lookup (first value wins; hash already checked)
    fields = {k: v for k, v in pairs if k != "hash"}
    try:
        auth_date = int(fields.get("auth_date") or 0)
    except (TypeError, ValueError):
        raise InitDataError("bad auth_date") from None
    if auth_date <= 0:
        raise InitDataError("bad auth_date")
    age = time.time() - auth_date
    if age > max_age_sec or age < -60:
        # allow 60s clock skew forward; reject old / far-future
        raise InitDataError("expired auth_date")

    user_raw = fields.get("user") or ""
    if not user_raw:
        raise InitDataError("missing user")
    try:
        user = json.loads(user_raw)
    except Exception as exc:
        raise InitDataError("bad user json") from exc
    if not isinstance(user, dict):
        raise InitDataError("bad user")
    try:
        user_id = int(user.get("id"))
    except (TypeError, ValueError):
        raise InitDataError("bad user id") from None
    if user_id <= 0:
        raise InitDataError("bad user id")

    username = (user.get("username") or "") or None
    if username is not None:
        username = str(username).strip() or None
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    full_name = f"{first} {last}".strip() or first or last or None

    return {
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "auth_date": auth_date,
    }


def build_customer_order_received_text(
    order: dict,
    shop_chat_id: int,
    *,
    emoji: str = "\U0001f6cd",
    markdown: bool = False,
    order_lines: list[dict] | None = None,
) -> str:
    """Customer 'Order received' body (sendData path + POST /order).

    When markdown=True, product/pay strings are escaped for Telegram legacy
    Markdown. HTTP path uses markdown=False + parse_mode=None.
    """
    oid = int(order["id"])
    if order_lines is None:
        order_lines = db.get_order_items(oid)

    def _esc(s: str) -> str:
        return _md_escape(s) if markdown else s

    lines: list[str] = []
    for ln in order_lines or []:
        lines.append(
            f"  • {_esc(str(ln['product_name']))} × {ln['quantity']} — "
            f"{_fmt_money(ln['line_total'])}"
        )
    if order.get("shipping_fee"):
        lines.append(f"  • Shipping — {_fmt_money(order['shipping_fee'])}")

    ship_name = (order.get("ship_name") or "").strip()
    ship_address = (order.get("ship_address") or "").strip()
    ship_block = format_customer_ship_block(
        ship_name, ship_address, markdown=markdown
    )

    pays = db.list_payment_methods(int(shop_chat_id))
    if pays:
        pay_lines = [
            f"  • {_esc(p['name'])}: {_esc(p['instructions'])}".rstrip(": ")
            for p in pays
        ]
        pay_txt = "\n".join(pay_lines)
    else:
        pay_txt = "  • Payment details will be DM'd to you."

    code = order.get("payment_code") or f"#{oid}"
    total_txt = _fmt_money(order.get("total", 0))

    if markdown:
        return (
            f"{emoji} *Order received!*\n\n"
            + "\n".join(lines)
            + ship_block
            + f"\n\n*Total: {total_txt}*\n"
            + f"Payment code: `{_esc(code)}` (put this in the memo)\n\n"
            + "Pay with:\n"
            + pay_txt
            + "\n\n"
            + "You'll get a confirmation here once payment lands."
        )
    return (
        f"{emoji} Order received!\n\n"
        + "\n".join(lines)
        + ship_block
        + f"\n\nTotal: {total_txt}\n"
        + f"Payment code:\n{code}\n"
        + "(put this in the memo)\n\n"
        + "Pay with:\n"
        + pay_txt
        + "\n\n"
        + "You'll get a confirmation here once payment lands."
    )


def payment_display_lines(shop_chat_id: int) -> list[str]:
    """Payment method lines for JSON responses (name: instructions)."""
    pays = db.list_payment_methods(int(shop_chat_id))
    return [
        f"{p['name']}: {p['instructions']}".rstrip(": ") for p in pays
    ]


def _h(s) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html_mod.escape(str(s or ""), quote=False)


_PAY_TARGET_RE = re.compile(
    r"(@[A-Za-z0-9_.\-]+|\$[A-Za-z0-9_]+|[\w.+-]+@[\w-]+\.[\w.]+)"
)


def _method_kind_and_target(method: dict) -> tuple[str, str]:
    """(method_type, pay target) — inferred for legacy instructions-only rows."""
    mt = (method.get("method_type") or "").lower().strip()
    if not mt:
        name = (method.get("name") or "").lower()
        for kind, needle in (
            ("venmo", "venmo"),
            ("cashapp", "cash"),
            ("paypal", "paypal"),
            ("zelle", "zelle"),
            ("apple_cash", "apple"),
        ):
            if needle in name:
                mt = kind
                break
    target = (
        method.get("cashtag") or method.get("handle") or method.get("address") or ""
    ).strip()
    if not target and mt:
        m = _PAY_TARGET_RE.search(method.get("instructions") or "")
        if m:
            target = m.group(1)
    return mt, target


def payment_pay_link(method: dict, total: float, code: str) -> str | None:
    """Deep link that pre-fills the amount (and note where supported).

    Venmo pre-fills amount + order code in the note; Cash App and PayPal.me
    pre-fill the amount only. Email PayPal / Zelle / Apple Cash / crypto /
    custom have no universal pay URL → None.
    """
    mt, target = _method_kind_and_target(method)
    if not target:
        return None
    amt = f"{float(total):.2f}"
    q = urllib.parse.quote
    if mt == "venmo":
        h = target.lstrip("@$")
        if h:
            # account.venmo.com is Venmo's canonical web pay URL — opens the
            # site, which hands off to the app with amount + note prefilled.
            return (
                "https://account.venmo.com/pay?txn=pay"
                f"&recipients={q(h)}&amount={amt}&note={q(code)}"
            )
    elif mt == "cashapp":
        tag = target.lstrip("@")
        if tag:
            if not tag.startswith("$"):
                tag = "$" + tag
            return f"https://cash.app/{q(tag)}/{amt}"
    elif mt == "paypal":
        h = target.lstrip("@")
        # paypal.me only works for usernames, not emails
        if h and "@" not in h:
            return f"https://paypal.me/{q(h)}/{amt}"
    return None


def _payment_method_html(p: dict, total: float, code: str) -> str:
    """One payment method as Telegram-HTML lines with tap-to-copy target."""
    mt, target = _method_kind_and_target(p)
    lines = [f"• <b>{_h(p.get('name') or 'Payment')}</b>"]
    if target:
        lines[0] += f" — send to <code>{_h(target)}</code>"
    if mt == "paypal":
        ff = (p.get("network_note") or "friends_family") == "friends_family"
        lines.append(
            "   ⚠️ Send as <b>Friends &amp; Family</b>" if ff
            else "   Send as <b>Goods &amp; Services</b>"
        )
    elif mt == "crypto":
        if p.get("network_note"):
            lines.append(f"   Network: {_h(p['network_note'])}")
        lines.append("   ⚠️ Double-check the network before sending.")
    elif not target:
        # custom / free-text method: show its instructions verbatim (escaped)
        instr = (p.get("instructions") or "").strip()
        if instr:
            lines.append(f"   {_h(instr)}")
    link = payment_pay_link(p, total, code)
    if link:
        prefills = "amount + order code" if mt == "venmo" else "amount"
        lines.append(
            f'   👉 <a href="{html_mod.escape(link)}">Tap to pay '
            f"{_fmt_money(total)}</a> ({prefills} prefilled)"
        )
    return "\n".join(lines)


def build_customer_order_received_html(
    order: dict,
    shop_chat_id: int,
    *,
    emoji: str = "\U0001f6cd",
    order_lines: list[dict] | None = None,
) -> str:
    """Telegram-HTML customer receipt: tap-to-copy payment code + pay links."""
    oid = int(order["id"])
    if order_lines is None:
        order_lines = db.get_order_items(oid)
    code = order.get("payment_code") or f"#{oid}"
    total = float(order.get("total") or 0)

    lines = [
        f"  • {_h(ln['product_name'])} × {ln['quantity']} — "
        f"{_fmt_money(ln['line_total'])}"
        for ln in order_lines or []
    ]
    if order.get("shipping_fee"):
        lines.append(f"  • Shipping — {_fmt_money(order['shipping_fee'])}")

    ship_bits = []
    if (order.get("ship_name") or "").strip():
        ship_bits.append(f"  {_h(order['ship_name'].strip())}")
    if (order.get("ship_address") or "").strip():
        ship_bits.append(f"  {_h(order['ship_address'].strip())}")
    ship_block = ("\n\n📦 Ship to:\n" + "\n".join(ship_bits)) if ship_bits else ""

    pays = db.list_payment_methods(int(shop_chat_id))
    if pays:
        pay_txt = "\n".join(_payment_method_html(p, total, code) for p in pays)
    else:
        pay_txt = "  • Payment details will be DM'd to you."

    return (
        f"{emoji} <b>Order received!</b>\n\n"
        + "\n".join(lines)
        + ship_block
        + f"\n\n<b>Total: {_fmt_money(total)}</b>\n"
        + f"Order code: <code>{_h(code)}</code>\n"
        + "(tap the code to copy it, then paste it in the payment note)\n\n"
        + "Pay with:\n"
        + pay_txt
        + "\n\nYou'll get a confirmation here once payment lands."
    )


def build_payment_qr_photos(
    shop_chat_id: int, total: float, code: str
) -> list[tuple[bytes, str]]:
    """(png_bytes, html_caption) per link-capable payment method.

    Scannable from another device — the QR encodes the same prefilled pay
    link. Returns [] if the optional `segno` dependency is missing.
    """
    try:
        import segno
    except ImportError:
        log.warning("segno not installed — skipping payment QR codes")
        return []
    out: list[tuple[bytes, str]] = []
    for p in db.list_payment_methods(int(shop_chat_id)):
        link = payment_pay_link(p, total, code)
        if not link:
            continue
        buf = io.BytesIO()
        try:
            segno.make(link, error="m").save(
                buf, kind="png", scale=8, border=2
            )
        except Exception:
            log.exception("QR render failed for method %s", p.get("name"))
            continue
        cap = (
            f"📱 Scan to pay <b>{_fmt_money(total)}</b> via "
            f"{_h(p.get('name') or 'link')} — order "
            f"<code>{_h(code)}</code>\n"
            "Scan with your phone's <b>camera app</b> (not the scanner "
            "inside the payment app) — it opens the pay page with the "
            "amount filled in."
        )
        out.append((buf.getvalue(), cap))
    return out


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


def build_new_order_notify_text(
    order: dict,
    *,
    shop_name: str = "",
    emoji: str = "\U0001f6cd",
    order_lines: list[dict] | None = None,
) -> str:
    """Full NEW ORDER vendor/owner DM text (plain). Shared by on_web_app_data + /resend.

    Includes items, total, ship-to, and confirm+track+cancel links when
    PANEL_BASE_URL is set (mint/reuse tokens via webpanel).
    """
    oid = int(order["id"])
    code = order.get("payment_code") or f"#{oid}"
    full_name = (order.get("full_name") or "").strip() or "Customer"
    username = (order.get("username") or "").strip() or "—"
    user_id = order.get("user_id") or "—"
    shop_label = (shop_name or "").strip() or "the shop"

    if order_lines is None:
        order_lines = db.get_order_items(oid)
    plain_lines = [
        f"  • {ln['product_name']} × {ln['quantity']} — {_fmt_money(ln['line_total'])}"
        for ln in (order_lines or [])
    ]
    if order.get("shipping_fee"):
        plain_lines.append(f"  • Shipping — {_fmt_money(order['shipping_fee'])}")

    total_txt = _fmt_money(order.get("total", 0))
    ship_notify = format_new_order_ship_section(
        (order.get("ship_name") or "").strip(),
        (order.get("ship_address") or "").strip(),
    )

    confirm_line = ""
    track_line = ""
    cancel_line = ""
    try:
        import webpanel as _webpanel

        shop_chat_id = int(order["chat_id"])
        confirm_line = _webpanel.format_confirm_payment_dm_line(oid, shop_chat_id)
        track_line = _webpanel.format_add_tracking_dm_line(oid, shop_chat_id)
        cancel_line = _webpanel.format_cancel_order_dm_line(oid, shop_chat_id)
    except Exception:
        log.exception(
            "mint order action links failed for order %s", order.get("id")
        )

    note = (
        f"{emoji} NEW ORDER {code} — {shop_label}\n"
        f"From: {full_name} (@{username}, id {user_id})\n"
        + "\n".join(plain_lines)
        + f"\nTotal: {total_txt}\n"
        + ship_notify
    )
    if confirm_line:
        note = note + "\n" + confirm_line
    if track_line:
        note = note + "\n" + track_line
    if cancel_line:
        note = note + "\n" + cancel_line
    return note


async def notify_order_recipient(
    shop_chat_id,
    recipient_id,
    text: str,
    context=None,
) -> bool:
    """Deliver one NEW ORDER DM: vendor storefront bot first, then main SPBC bot.

    Recipients may have /start'd *either* the shop's vendor bot or @SPBCOrderBot
    (claim link). Trying both avoids silent failure when only one is opened.

    1) get_bot_token_for_shop → webpanel.telegram_send_with_token (parse_mode=None)
    2) On fail/False → spbc_notify.send_telegram (main bot)

    Both sends are sync HTTP; run via asyncio.to_thread. Never raises.
    ``context`` is accepted for call-site flexibility (unused; delivery is token-based).
    """
    _ = context
    try:
        rid = int(recipient_id)
        shop = int(shop_chat_id)
    except (TypeError, ValueError):
        log.warning(
            "notify_order_recipient: bad ids shop=%r recipient=%r",
            shop_chat_id,
            recipient_id,
        )
        return False

    # 1) Vendor storefront bot token
    vendor_ok = False
    try:
        import webpanel as _webpanel

        token = get_bot_token_for_shop(shop)
        if token:
            vendor_ok = bool(
                await asyncio.to_thread(
                    _webpanel.telegram_send_with_token,
                    token,
                    rid,
                    text,
                    parse_mode=None,
                )
            )
            if vendor_ok:
                log.info(
                    "notify_order_recipient: delivered via vendor bot shop=%s to=%s",
                    shop,
                    rid,
                )
                return True
            log.info(
                "notify_order_recipient: vendor bot send failed shop=%s to=%s; "
                "trying main bot",
                shop,
                rid,
            )
        else:
            log.info(
                "notify_order_recipient: no vendor token for shop=%s; trying main bot",
                shop,
            )
    except Exception:
        log.exception(
            "notify_order_recipient: vendor path error shop=%s to=%s",
            shop,
            rid,
        )

    # 2) Main bot fallback (@SPBCOrderBot / pool token)
    try:
        import spbc_notify

        await asyncio.to_thread(spbc_notify.send_telegram, rid, text)
        log.info(
            "notify_order_recipient: delivered via main bot shop=%s to=%s",
            shop,
            rid,
        )
        return True
    except Exception as e:
        log.warning(
            "notify_order_recipient: both paths failed shop=%s to=%s: %s",
            shop,
            rid,
            e,
        )
        return False


def vendor_meta_for_shop(shop_chat_id: int) -> dict:
    """Brand + configured notify_ids for a shop (from vendor config or shop title)."""
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
            continue
        if resolved and int(resolved) == target:
            raw_ids = v.get("notify_ids") or []
            ids: list[int] = []
            for x in raw_ids:
                try:
                    ids.append(int(x))
                except (TypeError, ValueError):
                    continue
            return {
                "name": (v.get("name") or "").strip() or "the shop",
                "emoji": (v.get("emoji") or "\U0001f6cd"),
                "notify_ids": ids,
            }
    shop = db.get_shop(target) or {}
    return {
        "name": (shop.get("title") or "").strip() or "the shop",
        "emoji": "\U0001f6cd",
        "notify_ids": [],
    }


def base_notify_ids_for_shop(shop_chat_id: int) -> list[int]:
    """Configured notify_ids + owner (same base set as on_web_app_data)."""
    meta = vendor_meta_for_shop(shop_chat_id)
    return list(dict.fromkeys([*meta["notify_ids"], *_owner_ids()]))


def find_order_for_resend(key: str) -> dict | None:
    """Look up an order by payment_code or numeric id (optional leading #)."""
    raw = (key or "").strip()
    if not raw:
        return None
    # Numeric id (allow #123)
    id_part = raw[1:] if raw.startswith("#") and len(raw) > 1 else raw
    if id_part.isdigit():
        order = db.get_order(int(id_part))
        if order:
            return order
    return db.get_order_by_payment_code(raw)


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
            items = parse_miniapp_cart_items(parsed.get("items"))
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
        html_text = build_customer_order_received_html(
            order,
            shop_chat_id,
            emoji=emoji,
            order_lines=order_lines,
        )
        plain_text = build_customer_order_received_text(
            order,
            shop_chat_id,
            emoji=emoji,
            markdown=False,
            order_lines=order_lines,
        )

        # Customer reply must not block owner notify if Telegram rejects HTML.
        try:
            await msg.reply_text(
                html_text, parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            log.warning("[%s] customer confirm reply failed (order still saved): %s", name, e)
            try:
                await msg.reply_text(plain_text)
            except Exception as e2:
                log.warning("[%s] plain customer confirm also failed: %s", name, e2)

        # Owner/vendor NEW ORDER: try vendor storefront bot, then main bot.
        # Recipients may have started either bot (storefront or claim via SPBC).
        note = build_new_order_notify_text(
            order,
            shop_name=name,
            emoji=emoji,
            order_lines=order_lines,
        )
        recipients = build_notify_recipient_ids(notify_ids, shop_chat_id)
        for nid in recipients:
            await notify_order_recipient(shop_chat_id, nid, note, context=context)

    async def on_offer_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Accept/Decline an SPBC fulfillment offer from inside the vendor's bot.

        Offers are delivered through this bot, so the taps land here rather
        than on the main SPBC bot. Stock only moves — and the customer's
        address is only released — on Accept.
        """
        import order_router

        query = update.callback_query
        user = update.effective_user
        action, _, quote_id = (query.data or "").partition(":")
        allowed, why, _q = order_router.can_accept(quote_id, user.id if user else 0)
        if not allowed:
            await query.answer(why, show_alert=True)
            return

        owner_ids = _owner_ids()
        if action == "voffer_no":
            ok, msg, quote = order_router.decline_quote(quote_id, user.id)
            if not ok:
                await query.answer(msg, show_alert=True)
                return
            await query.answer("Declined — thanks for the quick answer.")
            await query.edit_message_text(
                f"❌ Declined order {quote['order_number']}. Nothing changed on "
                "your side — SPBC will ask someone else."
            )
            for oid in owner_ids:
                await notify_order_recipient(
                    quote["shop_chat_id"],
                    oid,
                    f"❌ {quote['shop_title']} declined order "
                    f"{quote['order_number']}. No stock moved, no address shared.",
                    context=context,
                )
            return

        ok, msg, quote = order_router.apply_route(quote_id, user.id)
        if not ok:
            await query.answer(msg, show_alert=True)
            return
        await query.answer("Accepted ✅")
        # Full details INCLUDING the shipping address, now that they committed
        await query.edit_message_text(order_router.build_vendor_message(quote))
        order_router.dismiss_order(quote["order_number"])
        for oid in owner_ids:
            await notify_order_recipient(
                quote["shop_chat_id"],
                oid,
                f"✅ {quote['shop_title']} accepted order "
                f"{quote['order_number']} — ${quote['total']:.2f}. "
                "Stock deducted, address sent to them.",
                context=context,
            )

    app = Application.builder().token(v["token"]).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    # SPBC fulfillment offers arrive through THIS bot, so Accept/Decline taps
    # come back here — not to the main SPBC bot.
    app.add_handler(CallbackQueryHandler(on_offer_answer, pattern=r"^voffer_(ok|no):"))

    async def on_supplier_handoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Answer a website order handed to this vendor as their supplier."""
        import spbc_notify as _sn

        query = update.callback_query
        action, _, hid = (query.data or "").partition(":")
        state = "accepted" if action == "shand_ok" else "declined"
        h = _sn.set_handoff_state(hid, state)
        if not h:
            await query.answer("Already answered — thanks.", show_alert=True)
            return
        lines = [f"• {it['qty']}× {it['name']}" for it in h.get("items") or []]
        if state == "accepted":
            await query.answer("Thanks — marked as on it ✅")
            await query.edit_message_text(
                f"✅ You're filling order {h['order_number']}.\n"
                + "\n".join(lines)
                + "\n\nShip to the address above. The shop owner has been told."
            )
            note = (
                f"✅ {h['supplier']} is filling order {h['order_number']}.\n"
                + "\n".join(lines)
            )
        else:
            await query.answer("Noted — the owner will re-source it.")
            await query.edit_message_text(
                f"❌ Marked as unable to fill order {h['order_number']}. "
                "The shop owner will source it elsewhere."
            )
            note = (
                f"❌ {h['supplier']} can't fill order {h['order_number']} — "
                "needs re-sourcing.\n" + "\n".join(lines)
            )
        for oid in _owner_ids():
            await notify_order_recipient(
                h["shop_chat_id"], oid, note, context=context
            )

    app.add_handler(
        CallbackQueryHandler(on_supplier_handoff, pattern=r"^shand_(ok|no):")
    )
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


def vendor_bot_for_user(user_id: int | str) -> dict | None:
    """Find the vendor storefront bot a person runs, by their Telegram id.

    Website suppliers are identified by `suppliers.telegram_chat_id`. If that
    same person administers a vendor shop that has its own bot, an order for
    them can be handed off through THEIR bot (branded, actionable) instead of
    a plain notification from the main SPBC bot.

    Returns {shop_chat_id, token, name, emoji} or None when unmapped.
    """
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        return None
    for v in load_vendor_configs():
        token = (v.get("token") or "").strip()
        if not token:
            continue
        try:
            shop = _resolve_shop(v)
        except Exception:
            continue
        if not shop:
            continue
        try:
            admins = {int(a["user_id"]) for a in db.list_admins(int(shop))}
        except Exception:
            admins = set()
        notify_ids = set()
        for x in v.get("notify_ids") or []:
            try:
                notify_ids.add(int(x))
            except (TypeError, ValueError):
                continue
        if uid in admins or uid in notify_ids:
            return {
                "shop_chat_id": int(shop),
                "token": token,
                "name": (v.get("name") or "").strip() or "your shop",
                "emoji": v.get("emoji") or "\U0001f6cd",
            }
    return None


if __name__ == "__main__":
    # Standalone/local test mode: run all configured vendors in the foreground.
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    n = start_all()
    if not n:
        raise SystemExit("no vendors configured (VENDOR_STORES_JSON or UNICORN_* env)")
    threading.Event().wait()
