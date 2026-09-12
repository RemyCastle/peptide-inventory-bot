"""SPBC supplier-notify service (port of the Node spbc-supplier-bot).

Adds the website → Telegram order pipeline to this bot:

- POST /notify   (X-Notify-Secret) from the spbc-orders Cloudflare worker
    status pending|placed|received      → owner-only phone alert (prices OK)
    status paid|shipped|complete        → ONE message PER supplier (no prices),
                                          then ask each supplier for total + OOS
- POST /order    (public, Telegram initData auth) → mini-app checkout for all
    launch modes (startapp / home / menu / keyboard); CORS for Pages origin
- OPTIONS /order → CORS preflight
- POST /resolve-chat (secret)  → username → chat id (getChat + recent-chat fallback)
- GET  /recent-chats (secret)  → chats that recently messaged the bot (admin picker)
- GET  / and /health           → status JSON (Render health check, no secret)

Supplier Q&A (total / out-of-stock) runs through the main PTB update stream:
`on_supplier_message` is registered in a NEGATIVE handler group so an active
supplier session wins over the shop conversation handlers, and is a no-op for
everyone else. The HTTP server runs on a daemon thread (see run_cloud.py) and
talks to Telegram directly over HTTPS, so it never touches the PTB event loop.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import (
    NOTIFY_SECRET,
    OWNER_TELEGRAM_CHAT_ID,
    SUPPLIER_TELEGRAM_CHAT_ID,
)

log = logging.getLogger("spbc_notify")

PLACED_STATUSES = {"pending", "placed", "received", "order_received"}
PAID_STATUSES = {"paid", "shipped", "complete"}

SESSION_TTL_SEC = 48 * 60 * 60
MAX_BODY_BYTES = 9 * 1024 * 1024  # panel photo uploads travel as base64 JSON
RECENT_CHATS_CAP = 200

# ── Shared state (HTTP thread + PTB loop) ────────────────────────────────────
_state_lock = threading.Lock()
_bot_token: str = ""
# chat_id(str) -> {step, order_number, supplier, items, total, oos, started_at}
_sessions: dict[str, dict] = {}
# chat_id(str) -> {telegram_chat_id, username, first_name, title, type}
_recent_chats: "OrderedDict[str, dict]" = OrderedDict()
_NOTIFY_STAT_KEYS = (
    "order_ok",
    "order_fail",
    "order_empty_recipients",
    "claim_ok",
    "claim_fail",
    "claim_empty_recipients",
)
_notify_stats: dict[str, int] = {k: 0 for k in _NOTIFY_STAT_KEYS}


def set_bot_token(token: str) -> None:
    """Called from build_app once the active token is known."""
    global _bot_token
    with _state_lock:
        _bot_token = (token or "").strip()


def open_session_count() -> int:
    with _state_lock:
        return len(_sessions)


class NotifyError(Exception):
    def __init__(self, message: str, code: str = "send_failed"):
        super().__init__(message)
        self.code = code


# ── Direct Telegram HTTP API (for the HTTP-server thread) ────────────────────

def _telegram_api(method: str, body: dict | None = None) -> dict:
    with _state_lock:
        token = _bot_token
    if not token:
        raise NotifyError("Bot token not ready", code="misconfigured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": f"Telegram HTTP {exc.code}"}
    except Exception as exc:  # network errors
        return {"ok": False, "description": str(exc)}


def send_telegram(chat_id: str | int, text: str) -> dict:
    """Send a message from the HTTP thread. Raises NotifyError on failure."""
    if not chat_id:
        raise NotifyError("No telegram chat id for this supplier", code="no_chat_id")
    body = text if len(text) <= 4000 else text[:3990] + "\n…"
    data = _telegram_api(
        "sendMessage",
        {"chat_id": chat_id, "text": body, "disable_web_page_preview": True},
    )
    if not data.get("ok"):
        raise NotifyError(
            data.get("description") or "Telegram send failed", code="telegram_failed"
        )
    return data.get("result") or {}


def notify_stats() -> dict[str, int]:
    """Process-lifetime Mini App staff-notify counters for /health."""
    with _state_lock:
        return {k: int(_notify_stats.get(k) or 0) for k in _NOTIFY_STAT_KEYS}


def _bump_notify(key: str) -> None:
    with _state_lock:
        _notify_stats[key] = int(_notify_stats.get(key) or 0) + 1


def _live_bot_token() -> str:
    """Poller token, then TELEGRAM_BOT_TOKEN env/config."""
    with _state_lock:
        tok = (_bot_token or "").strip()
    if tok:
        return tok
    env = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if env:
        return env
    try:
        from config import TELEGRAM_BOT_TOKEN as _cfg

        return (_cfg or "").strip()
    except Exception:
        return ""


def _owner_chat_id() -> int | None:
    raw = str(OWNER_TELEGRAM_CHAT_ID or "").strip()
    if not raw:
        raw = (os.getenv("OWNER_TELEGRAM_CHAT_ID") or "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    try:
        import vendor_stores

        ids = vendor_stores._owner_ids()
        if ids:
            return int(ids[0])
    except Exception:
        pass
    return None


def _deliver_staff_dm(
    rid: int,
    text: str,
    vendor_token: str,
    *,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
    log_label: str = "notify",
) -> bool:
    """Vendor HMAC token first, then TELEGRAM_BOT_TOKEN / poller. Never skip Unicorn."""
    import webpanel

    def try_token(tok: str, why: str, tried: list[str]) -> bool:
        tok = (tok or "").strip()
        if not tok or tok in tried:
            return False
        tried.append(tok)
        try:
            ok = bool(
                webpanel.telegram_send_with_token(
                    tok,
                    rid,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
            )
        except Exception:
            log.exception("%s %s send error rid=%s", log_label, why, rid)
            return False
        if ok:
            log.info("%s notified rid=%s via %s", log_label, rid, why)
            return True
        log.info("%s %s failed rid=%s", log_label, why, rid)
        return False

    tried: list[str] = []
    if try_token(vendor_token, "vendor_token", tried):
        return True
    live = _live_bot_token()
    if try_token(live, "TELEGRAM_BOT_TOKEN", tried):
        return True
    try:
        send_telegram(rid, text)
        log.info("%s notified rid=%s via main bot", log_label, rid)
        return True
    except Exception as exc:
        log.warning("%s main bot failed rid=%s: %s", log_label, rid, exc)
        return False


def _notify_staff(
    shop_chat_id: int,
    vendor_token: str,
    text: str,
    *,
    order_id: Any = None,
    kind: str = "order",
    log_label: str = "POST /order",
    reply_markup: dict | None = None,
) -> bool:
    """DM shop staff. Empty recipients and notified_any=false escalate to OWNER."""
    import vendor_stores

    base_ids = vendor_stores.base_notify_ids_for_shop(shop_chat_id)
    recipients = vendor_stores.build_notify_recipient_ids(base_ids, shop_chat_id)
    if not recipients:
        log.warning(
            "%s empty recipients shop=%s order=%s — escalating to OWNER",
            log_label,
            shop_chat_id,
            order_id,
        )
        _bump_notify(f"{kind}_empty_recipients")
        owner = _owner_chat_id()
        if owner:
            recipients = [owner]
        else:
            log.warning(
                "%s empty recipients and no OWNER_TELEGRAM_CHAT_ID "
                "shop=%s order=%s",
                log_label,
                shop_chat_id,
                order_id,
            )
            _bump_notify(f"{kind}_fail")
            return False

    notified_any = False
    for rid in recipients:
        try:
            ok = _deliver_staff_dm(
                int(rid),
                text,
                vendor_token,
                parse_mode=None,
                reply_markup=reply_markup,
                log_label=log_label,
            )
        except Exception:
            log.exception("%s deliver error shop=%s rid=%s", log_label, shop_chat_id, rid)
            ok = False
        if ok:
            notified_any = True

    if not notified_any:
        log.warning(
            "%s notified_any=false shop=%s order=%s — escalating",
            log_label,
            shop_chat_id,
            order_id,
        )
        owner = _owner_chat_id()
        seen = set()
        for rid in recipients:
            try:
                seen.add(int(rid))
            except (TypeError, ValueError):
                continue
        if owner and owner not in seen:
            if _deliver_staff_dm(
                owner,
                text,
                vendor_token,
                parse_mode=None,
                reply_markup=reply_markup,
                log_label=f"{log_label} owner-escalate",
            ):
                notified_any = True
        if not notified_any:
            _bump_notify(f"{kind}_fail")
            return False

    _bump_notify(f"{kind}_ok")
    return True


# ── Payload helpers (faithful port of server.js) ─────────────────────────────

_KIND_SUFFIXES = ("(vial)", "(kit)", "(10-pack / kit)", "(10-pack)")


def strip_kind_suffix(name: Any) -> str:
    s = str(name or "").strip()
    low = s.lower()
    for suf in _KIND_SUFFIXES:
        if low.endswith(suf):
            return s[: len(s) - len(suf)].strip()
    return s


def match_source(sources: Any, item_name: str) -> Optional[str]:
    if not isinstance(sources, dict):
        return None
    base = strip_kind_suffix(item_name)
    if not base:
        return None
    if sources.get(base):
        return str(sources[base])
    if sources.get(item_name):
        return str(sources[item_name])
    lower = base.lower()
    for k, v in sources.items():
        if str(k).lower() == lower:
            return str(v)
    for k, v in sources.items():
        n = str(k).lower()
        if lower.startswith(n) or n.startswith(lower) or n in lower:
            return str(v)
    return None


def format_ship_lines(ship: Any) -> list[str]:
    """Address block shared by supplier messages and late address updates."""
    if not isinstance(ship, dict):
        return []
    city_line = ", ".join(
        str(v)
        for v in (ship.get("city"), ship.get("state"), ship.get("postal"))
        if v
    )
    out = []
    for s in (
        ship.get("name"),
        ship.get("line1"),
        ship.get("line2"),
        city_line,
        ship.get("country"),
        ship.get("phone"),
    ):
        if s:
            out.append(str(s))
    return out


def money_from_cents(cents: Any) -> Optional[str]:
    try:
        n = float(cents)
    except (TypeError, ValueError):
        return None
    return f"${n / 100:.2f}"


def group_items_by_supplier(payload: dict) -> "OrderedDict[str, dict]":
    """supplierName -> {supplier, chat_id, items:[{name, qty, notes}]}"""
    items = payload.get("items")
    items = items if isinstance(items, list) else []
    sources = payload.get("sources") or {}
    products_meta = payload.get("products") or {}
    suppliers_meta = payload.get("suppliers") or {}

    groups: "OrderedDict[str, dict]" = OrderedDict()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("sku") or "Item"
        try:
            qty = int(it.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        supplier = str(it.get("supplier") or it.get("supplier_name") or "").strip() or None
        notes = str(it.get("source") or it.get("notes") or "").strip() or None
        chat_id = str(it.get("telegram_chat_id") or "").strip() or None

        meta = None
        if isinstance(products_meta, dict):
            meta = products_meta.get(strip_kind_suffix(name)) or products_meta.get(name)
        if isinstance(meta, dict):
            supplier = supplier or (str(meta["supplier"]) if meta.get("supplier") else None)
            notes = notes or (str(meta["notes"]) if meta.get("notes") else None)
            chat_id = chat_id or (
                str(meta["telegram_chat_id"]).strip()
                if meta.get("telegram_chat_id")
                else None
            )
        if not supplier and not notes:
            matched = match_source(sources, str(name))
            if matched:
                supplier = matched.split("·")[0].strip()
        if not supplier:
            supplier = "(unassigned)"

        if not chat_id and isinstance(suppliers_meta, dict):
            smeta = suppliers_meta.get(supplier)
            if isinstance(smeta, dict) and smeta.get("telegram_chat_id"):
                chat_id = str(smeta["telegram_chat_id"]).strip()

        g = groups.setdefault(
            supplier, {"supplier": supplier, "chat_id": chat_id, "items": []}
        )
        if not g["chat_id"] and chat_id:
            g["chat_id"] = chat_id
        g["items"].append({"name": str(name), "qty": qty, "notes": notes})
    return groups


def _utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def build_owner_placed_message(payload: dict) -> str:
    order_number = payload.get("order_number") or payload.get("orderNumber") or "UNKNOWN"
    name = payload.get("customer_name") or payload.get("customerName") or "—"
    total = money_from_cents(
        payload.get("total_cents", payload.get("totalCents"))
    ) or (str(payload["total"]) if payload.get("total") is not None else None)
    items = payload.get("items")
    items = items if isinstance(items, list) else []

    lines = ["🆕 New SPBC order", f"Order: {order_number}", f"Customer: {name}"]
    if total:
        lines.append(f"Total: {total}")
    lines.append(f"Items: {len(items)}")
    lines.append("")
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = int(it.get("qty") or 0)
        label = it.get("name") or it.get("sku") or "Item"
        lines.append(f"• {qty}× {label}")
    if not items:
        lines.append("• (no line items in payload)")

    # Ship-to: usually absent at placed time (the form comes after ordering),
    # in which case notifyAddressUpdate pushes it the moment it arrives.
    ship = payload.get("shipping")
    ship_lines = format_ship_lines(ship)
    if ship_lines:
        lines += ["", "Ship to:", *ship_lines]
        if isinstance(ship, dict) and not ship.get("complete", True):
            lines.append("(customer hasn't finished the form — may change)")
    else:
        lines += ["", "Ship to: not provided yet — I'll send it when it lands."]

    lines += ["", "Status: awaiting payment", f"Sent {_utc_stamp()}"]
    return "\n".join(lines)


def build_supplier_message(payload: dict, group: dict) -> str:
    order_number = payload.get("order_number") or payload.get("orderNumber") or "UNKNOWN"
    status = payload.get("status") or "paid"
    ship = payload.get("shipping") or None
    note = str(payload.get("note") or "").strip()[:500]

    lines = [
        "🛒 SPBC order for you",
        f"Order: {order_number}",
        f"Status: {status}",
        f"Supplier: {group['supplier']}",
        "",
        "Your items:",
    ]
    for it in group["items"]:
        lines.append(f"• {it['qty']}× {it['name']}")
        notes = it.get("notes")
        if notes:
            clean = notes
            if isinstance(notes, str) and notes.startswith(group["supplier"]):
                clean = notes[len(group["supplier"]):].lstrip()
                clean = clean.lstrip("·-–").lstrip()
            if clean and clean != group["supplier"]:
                lines.append(f"  Notes: {clean}")

    if isinstance(ship, dict) and (ship.get("line1") or ship.get("name")):
        lines += ["", "Ship to:", *format_ship_lines(ship)]
        if not ship.get("complete", True):
            lines.append("(address not finalised by the customer yet)")
    else:
        lines += ["", "Shipping: (not on file yet)"]

    if note:
        lines += ["", f"Note: {note}"]
    lines += ["", f"Sent {_utc_stamp()} · no prices"]
    return "\n".join(lines)


_PRICE_LEAK_RE = re.compile(r"\$\s*\d|\d+\.\d{2}\s*USD|total_cents|unit_price", re.IGNORECASE)


def leaks_prices(text: str) -> bool:
    """Guard: supplier messages must never contain prices."""
    return bool(_PRICE_LEAK_RE.search(text))


# ── Supplier Q&A sessions ────────────────────────────────────────────────────

def _prune_sessions() -> None:
    now = time.time()
    with _state_lock:
        stale = [k for k, s in _sessions.items() if now - s.get("started_at", 0) > SESSION_TTL_SEC]
        for k in stale:
            _sessions.pop(k, None)


def _is_owner_chat(chat_id: Any) -> bool:
    return bool(OWNER_TELEGRAM_CHAT_ID) and str(chat_id) == str(OWNER_TELEGRAM_CHAT_ID)


def start_supplier_follow_up(
    chat_id: str, supplier: str, order_number: str, items: list[dict]
) -> dict:
    """Ask supplier for total, then OOS. Called from the HTTP thread."""
    if not chat_id or _is_owner_chat(chat_id):
        return {"started": False, "reason": "owner_or_missing_chat"}
    _prune_sessions()
    with _state_lock:
        _sessions[str(chat_id)] = {
            "step": "await_total",
            "order_number": order_number,
            "supplier": supplier or "supplier",
            "items": items if isinstance(items, list) else [],
            "total": None,
            "oos": None,
            "started_at": time.time(),
        }
    item_lines = "\n".join(f"• {it['qty']}× {it['name']}" for it in items or [])
    send_telegram(
        chat_id,
        "\n".join(
            [
                f"Order {order_number} — please reply with your TOTAL for these items.",
                "Examples: 450   or   $450.00",
                "",
                "Your lines:",
                item_lines or "• (see order message above)",
                "",
                "Next I will ask which items (if any) are out of stock.",
                "Commands: /cancel  /status",
            ]
        ),
    )
    return {"started": True}


def format_owner_report(session: dict, from_user: Any) -> str:
    who = "unknown"
    if from_user is not None:
        username = getattr(from_user, "username", None)
        if username:
            who = f"@{username}"
        else:
            who = " ".join(
                p
                for p in (
                    getattr(from_user, "first_name", None),
                    getattr(from_user, "last_name", None),
                )
                if p
            ) or "unknown"
    lines = [
        "📬 Supplier response",
        f"Order: {session['order_number']}",
        f"Supplier: {session['supplier']}",
        f"From: {who} (chat {session.get('chat_id') or 'n/a'})",
        "",
        f"Total: {session.get('total') or '(not given)'}",
        f"Out of stock: {session.get('oos') or '(not given)'}",
        "",
        "Items on order:",
    ]
    for it in session.get("items") or []:
        lines.append(f"• {it['qty']}× {it['name']}")
    lines += ["", f"Received {_utc_stamp()}"]
    return "\n".join(lines)


_TOTAL_RE = re.compile(r"^[\$€£]?\s*\d+(\.\d{1,2})?\s*(usd|dollars?)?$", re.IGNORECASE)
_OOS_NONE_RE = re.compile(
    r"^none$|^n/a$|^na$|^all good$|^all in stock$|^0$", re.IGNORECASE
)


def _looks_like_total(text: str) -> bool:
    return bool(_TOTAL_RE.match(text)) or len(text) <= 40


def _oos_is_none(text: str) -> bool:
    return bool(_OOS_NONE_RE.match(text))


async def on_supplier_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Supplier Q&A interceptor. Registered in a negative group in build_app:
    only acts when this private chat has an open supplier session, and then
    stops the update from reaching the shop handlers."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None or not msg.text or chat.type != "private":
        return
    key = str(chat.id)
    _prune_sessions()
    with _state_lock:
        session = _sessions.get(key)
    if session is None:
        return  # not a supplier mid-flow — let the shop handle it

    text = msg.text.strip()

    # /start keeps normal shop behavior even mid-session
    if text.lower().startswith("/start"):
        return

    if text.lower().startswith("/cancel"):
        with _state_lock:
            _sessions.pop(key, None)
        await msg.reply_text("Cancelled. No reply was sent to the owner.")
        raise ApplicationHandlerStop

    if text.lower().startswith("/status"):
        await msg.reply_text(
            f"Open: order {session['order_number']}\n"
            f"Step: {session['step']}\nSupplier: {session['supplier']}"
        )
        raise ApplicationHandlerStop

    session["chat_id"] = key

    if session["step"] == "await_total":
        cleaned = text.replace(",", "").strip()
        if not _looks_like_total(cleaned):
            await msg.reply_text(
                "Please send just the total amount (e.g. 450 or $450.00), or /cancel."
            )
            raise ApplicationHandlerStop
        session["total"] = cleaned
        session["step"] = "await_oos"
        with _state_lock:
            _sessions[key] = session
        await msg.reply_text(
            "\n".join(
                [
                    f"Got total: {cleaned}",
                    "",
                    "Any items you do NOT have in stock for this order?",
                    "Reply with the item names (or qty), or type: none",
                    "",
                    "Examples:",
                    "none",
                    "RETA 35 MG",
                    "1× SEMA 10MG, all BAC water",
                ]
            )
        )
        raise ApplicationHandlerStop

    if session["step"] == "await_oos":
        oos = "none" if _oos_is_none(text) else text
        session["oos"] = oos
        with _state_lock:
            _sessions.pop(key, None)
        await msg.reply_text(
            "\n".join(
                [
                    "Thanks — sent to the shop owner:",
                    f"Order {session['order_number']}",
                    f"Total: {session['total']}",
                    f"Out of stock: {oos}",
                ]
            )
        )
        if OWNER_TELEGRAM_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=OWNER_TELEGRAM_CHAT_ID,
                    text=format_owner_report(session, msg.from_user),
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                log.error("owner_notify_failed: %s", exc)
                await msg.reply_text(
                    "Could not reach the owner automatically — please message "
                    "them your total and OOS list."
                )
        raise ApplicationHandlerStop


# ── Recent-chat recorder (replaces Node getUpdates scans) ────────────────────

async def on_record_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Passive recorder in a high group; feeds /recent-chats and /resolve-chat."""
    chat = update.effective_chat
    if chat is None:
        return
    user = update.effective_user
    entry = {
        "telegram_chat_id": str(chat.id),
        "username": (user.username if user else None) or chat.username,
        "first_name": (user.first_name if user else None) or chat.first_name,
        "title": chat.title,
        "type": chat.type,
    }
    with _state_lock:
        _recent_chats.pop(str(chat.id), None)
        _recent_chats[str(chat.id)] = entry
        while len(_recent_chats) > RECENT_CHATS_CAP:
            _recent_chats.popitem(last=False)


def recent_chats() -> list[dict]:
    with _state_lock:
        return list(_recent_chats.values())[::-1]


# ── Supplier handoff into a vendor's own storefront bot ─────────────────────

# handoff_id -> {order_number, supplier, shop_chat_id, items, created_at, state}
_handoffs: dict[str, dict] = {}


def get_handoff(handoff_id: str) -> Optional[dict]:
    with _state_lock:
        h = _handoffs.get(handoff_id)
        return dict(h) if h else None


def set_handoff_state(handoff_id: str, state: str) -> Optional[dict]:
    with _state_lock:
        h = _handoffs.get(handoff_id)
        if not h or h.get("state") != "sent":
            return None
        h["state"] = state
        return dict(h)


def _handoff_to_vendor_bot(
    handoff: dict, chat_id: Any, text: str, order_number: str, group: dict
) -> dict:
    """Deliver a paid-order supplier message through the vendor's own bot.

    Same no-prices content, but branded and actionable: the vendor answers
    with a button instead of a free-text total, and the owner is told.
    """
    import secrets as _secrets

    import webpanel as _wp

    hid = _secrets.token_hex(6)
    with _state_lock:
        _handoffs[hid] = {
            "order_number": order_number,
            "supplier": group["supplier"],
            "shop_chat_id": handoff["shop_chat_id"],
            "items": group["items"],
            "created_at": time.time(),
            "state": "sent",
        }
    body = (
        f"{handoff['emoji']} {text}\n\n"
        "Can you fill this? Tap below — the shop owner is told either way."
    )
    ok = _wp.telegram_send_with_token(
        handoff["token"],
        chat_id,
        body,
        parse_mode=None,
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "✅ On it", "callback_data": f"shand_ok:{hid}"},
                    {"text": "❌ Can't fill", "callback_data": f"shand_no:{hid}"},
                ]
            ]
        },
    )
    if not ok:
        # Their bot didn't take it — fall back so the order is never lost
        log.warning("handoff via vendor bot failed, using main bot: %s", chat_id)
        with _state_lock:
            _handoffs.pop(hid, None)
        return send_telegram(chat_id, text)
    log.info(
        "supplier handoff via vendor bot order=%s supplier=%s shop=%s",
        order_number,
        group["supplier"],
        handoff["shop_chat_id"],
    )
    return {"message_id": None, "handoff_id": hid}


# ── /notify core (called by the HTTP handler) ────────────────────────────────

def handle_notify(payload: dict) -> tuple[int, dict]:
    status = str(payload.get("status") or "").lower()
    order_number = payload.get("order_number") or payload.get("orderNumber")
    if not order_number:
        return 400, {"error": "order_number required"}

    # A shipping address that arrived after the order was already routed:
    # forward it to whoever is fulfilling, else tell the owner.
    if status == "address_update":
        ship = payload.get("shipping")
        if not isinstance(ship, dict):
            return 400, {"error": "shipping required"}
        lines = [
            f"📬 Shipping address for order {order_number}",
            "",
            *format_ship_lines(ship),
        ]
        if not ship.get("complete", True):
            lines.append("\n(customer hasn't finished the form — may change)")
        text = "\n".join(lines)

        vendor = None
        try:
            import order_router

            vendor = order_router.routed_vendor(str(order_number))
        except Exception as exc:
            log.warning("routed_vendor lookup failed: %s", exc)

        sent_to = []
        if vendor:
            try:
                import db as _db
                import vendor_stores as _vs

                shop = int(vendor["shop_chat_id"])
                token = _vs.get_bot_token_for_shop(shop)
                targets = _vs.base_notify_ids_for_shop(shop) or [
                    int(a["user_id"]) for a in _db.list_admins(shop)
                ]
                for tid in targets:
                    ok = False
                    if token:
                        import webpanel as _wp

                        ok = _wp.telegram_send_with_token(
                            token, tid, text, parse_mode=None
                        )
                    if not ok:
                        try:
                            send_telegram(tid, text)
                            ok = True
                        except NotifyError:
                            ok = False
                    if ok:
                        sent_to.append(tid)
            except Exception as exc:
                log.error("address_update vendor delivery failed: %s", exc)

        owner = OWNER_TELEGRAM_CHAT_ID or SUPPLIER_TELEGRAM_CHAT_ID
        if owner:
            note = text
            if vendor:
                note += f"\n\nForwarded to {vendor['shop_title']}."
            else:
                note += "\n\nNo vendor has accepted this order yet."
            try:
                send_telegram(owner, note)
            except NotifyError:
                pass
        return 200, {
            "ok": True,
            "kind": "address_update",
            "order_number": order_number,
            "vendor": (vendor or {}).get("shop_title"),
            "messages_sent": len(sent_to) + (1 if owner else 0),
        }

    if status in PLACED_STATUSES:
        owner = OWNER_TELEGRAM_CHAT_ID or SUPPLIER_TELEGRAM_CHAT_ID
        if not owner:
            return 503, {
                "error": "misconfigured",
                "message": "OWNER_TELEGRAM_CHAT_ID (or SUPPLIER_TELEGRAM_CHAT_ID) "
                "required for placed-order alerts",
            }
        sent = send_telegram(owner, build_owner_placed_message(payload))
        return 200, {
            "ok": True,
            "kind": "owner_placed",
            "order_number": order_number,
            "messages_sent": 1,
            "telegram_message_id": sent.get("message_id"),
            "owner_chat_id": owner,
        }

    if status not in PAID_STATUSES:
        return 400, {
            "error": "status_not_supported",
            "message": "Use status pending/placed for owner alert, or "
            "paid/shipped/complete for suppliers.",
            "status": payload.get("status"),
        }

    # Partner-retail orders: supplier messages are batched (Thursday policy),
    # but vendor quote-and-suggest still runs so routing works for partners.
    if payload.get("quote_only"):
        suggested = False
        try:
            import order_router

            owner = OWNER_TELEGRAM_CHAT_ID or SUPPLIER_TELEGRAM_CHAT_ID
            if owner:
                spec = order_router.suggest_for_order(payload)
                if spec:
                    _telegram_api(
                        "sendMessage",
                        {
                            "chat_id": owner,
                            "text": spec["text"],
                            "reply_markup": spec["reply_markup"],
                            "disable_web_page_preview": True,
                        },
                    )
                    suggested = True
        except Exception as exc:
            log.warning("quote_only_suggest_failed: %s", exc)
        return 200, {
            "ok": True,
            "kind": "quote_only",
            "order_number": order_number,
            "vendor_quotes_suggested": suggested,
        }

    groups = group_items_by_supplier(payload)
    if not groups:
        return 400, {"error": "no_items", "message": "No line items"}

    results: list[dict] = []
    errors: list[dict] = []
    for group in groups.values():
        chat_id = group["chat_id"] or SUPPLIER_TELEGRAM_CHAT_ID or None
        text = build_supplier_message(payload, group)

        if leaks_prices(text):
            errors.append({"supplier": group["supplier"], "error": "prices_forbidden"})
            continue
        if not chat_id:
            errors.append(
                {
                    "supplier": group["supplier"],
                    "error": "no_telegram_chat_id",
                    "message": "Set Telegram chat id for this supplier in "
                    "SPBC admin → Suppliers",
                }
            )
            continue
        try:
            # If this supplier runs one of our vendor storefront bots, hand the
            # order off THERE — branded, with Accept / Can't-fill buttons —
            # instead of a plain notification from the main SPBC bot.
            handoff = None
            try:
                import vendor_stores as _vs

                handoff = _vs.vendor_bot_for_user(chat_id)
            except Exception as exc:
                log.warning("vendor bot lookup failed for %s: %s", chat_id, exc)

            if handoff:
                sent = _handoff_to_vendor_bot(
                    handoff, chat_id, text, str(order_number), group
                )
                results.append(
                    {
                        "supplier": group["supplier"],
                        "chat_id": chat_id,
                        "item_count": len(group["items"]),
                        "telegram_message_id": (sent or {}).get("message_id"),
                        "delivered_via": handoff["name"],
                        "handoff": True,
                        "ok": True,
                    }
                )
                continue

            sent = send_telegram(chat_id, text)
            follow_up = {"started": False}
            try:
                follow_up = start_supplier_follow_up(
                    str(chat_id), group["supplier"], str(order_number), group["items"]
                )
            except Exception as exc:
                log.warning(
                    "followup_start_failed supplier=%s: %s", group["supplier"], exc
                )
            results.append(
                {
                    "supplier": group["supplier"],
                    "chat_id": chat_id,
                    "item_count": len(group["items"]),
                    "telegram_message_id": sent.get("message_id"),
                    "follow_up_started": bool(follow_up.get("started")),
                    "ok": True,
                }
            )
        except NotifyError as exc:
            errors.append(
                {
                    "supplier": group["supplier"],
                    "chat_id": chat_id,
                    "error": exc.code,
                    "message": str(exc),
                }
            )

    # Quote-and-suggest: offer the owner any vendor shop that can fill the
    # whole order from bot stock. Advisory only — never blocks the response.
    suggestion_sent = False
    try:
        import order_router

        owner = OWNER_TELEGRAM_CHAT_ID or SUPPLIER_TELEGRAM_CHAT_ID
        if owner:
            spec = order_router.suggest_for_order(payload)
            if spec:
                _telegram_api(
                    "sendMessage",
                    {
                        "chat_id": owner,
                        "text": spec["text"],
                        "reply_markup": spec["reply_markup"],
                        "disable_web_page_preview": True,
                    },
                )
                suggestion_sent = True
    except Exception as exc:
        log.warning("route_suggest_failed: %s", exc)

    ok = len(results) > 0
    body: dict = {
        "ok": ok,
        "order_number": order_number,
        "messages_sent": len(results),
        "messages_failed": len(errors),
        "results": results,
        "vendor_quotes_suggested": suggestion_sent,
    }
    if errors:
        body["errors"] = errors
    return (200 if ok else 502), body


def handle_resolve_chat(payload: dict) -> tuple[int, dict]:
    username = str(payload.get("username") or payload.get("telegram_username") or "")
    username = username.strip().lstrip("@")
    if not username:
        return 400, {
            "error": "username_required",
            "message": "Provide username (e.g. unicornfartzz or @unicornfartzz)",
        }
    data = _telegram_api("getChat", {"chat_id": f"@{username}"})
    if data.get("ok") and (data.get("result") or {}).get("id") is not None:
        r = data["result"]
        return 200, {
            "ok": True,
            "method": "getChat",
            "telegram_chat_id": str(r["id"]),
            "username": r.get("username") or username,
            "first_name": r.get("first_name"),
            "last_name": r.get("last_name"),
            "title": r.get("title"),
            "type": r.get("type"),
        }
    # Fallback: recent chats recorded from the live update stream
    want = username.lower()
    for entry in recent_chats():
        if (entry.get("username") or "").lower() == want:
            return 200, {
                "ok": True,
                "method": "recent_chats",
                **entry,
                "hint": "Matched from a recent message to the bot",
            }
    return 404, {
        "ok": False,
        "error": "not_found",
        "message": data.get("description")
        or f"Could not resolve @{username}. They must open your bot and press "
        "Start first, then try Lookup again.",
        "telegram_error": data.get("description"),
    }


# ── HTTP server ──────────────────────────────────────────────────────────────

ORDER_STOCK_ERROR = (
    "Couldn't place that order — something in your cart just sold "
    "out or the quantity isn't available. Reopen the store to see live stock."
)


def _catalog_shop_bound() -> bool:
    """True when live inventory.db has a Unicorn catalog shop (no SPBC chat required)."""
    try:
        import unicorn_shop

        return unicorn_shop.find_catalog_shop() is not None
    except Exception:
        return False


def live_git_sha() -> str:
    """Commit Render built this process from. Empty when not a Render deploy."""
    for key in ("RENDER_GIT_COMMIT", "GIT_COMMIT"):
        raw = (os.getenv(key) or "").strip()
        if raw:
            return raw[:40]
    return ""


_catalog_cleanup_last: dict[str, Any] | None = None
_keep_only_last: dict[str, Any] | None = None


def set_catalog_cleanup_result(result: dict | None) -> None:
    """Remember last Unicorn catalog cleanup for /health (no chat ids)."""
    global _catalog_cleanup_last
    if not result:
        _catalog_cleanup_last = None
        return
    out: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "renames": int(result.get("renames") or 0),
        "merges": int(result.get("merges") or 0),
        "deactivated": int(result.get("deactivated") or 0),
    }
    skipped = result.get("skipped")
    if skipped:
        out["skipped"] = str(skipped)[:80]
    msg = result.get("msg")
    if msg:
        out["msg"] = str(msg)[:200]
    shop_title = result.get("shop_title")
    if shop_title:
        out["shop_title"] = str(shop_title)[:80]
    _catalog_cleanup_last = out


def set_keep_only_result(result: dict | None) -> None:
    """Remember last keep-only catalog reattach for /health (counts only)."""
    global _keep_only_last
    if not result:
        _keep_only_last = None
        return
    out: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "moved": int(result.get("moved") or 0),
        "stray_shops": int(result.get("stray_shops") or 0),
    }
    skipped = result.get("skipped")
    if skipped:
        out["skipped"] = str(skipped)[:80]
    _keep_only_last = out


def _status_body() -> dict:
    with _state_lock:
        token_ok = bool(_bot_token)
        open_sessions = len(_sessions)
    catalog_bound = _catalog_shop_bound()
    body: dict[str, Any] = {
        "service": "unicornfartzz-bot",
        "ok": True,
        "mode": "combined_inventory_bot",
        "storefront_host": "https://unicornfartzz-bot.onrender.com",
        "telegram_bot_configured": token_ok,
        "telegram_configured": token_ok,
        "telegram_bot_ok": token_ok,
        # SPBC leftover used SUPPLIER_TELEGRAM_CHAT_ID. Unicorn-only deploys
        # treat a bound catalog shop as the default chat so /health is green
        # without restoring InventoryBot tokens or a supplier chat id.
        "default_chat_configured": bool(SUPPLIER_TELEGRAM_CHAT_ID) or catalog_bound,
        "owner_chat_configured": bool(
            OWNER_TELEGRAM_CHAT_ID or SUPPLIER_TELEGRAM_CHAT_ID
        )
        or catalog_bound,
        "notify_secret_configured": bool(NOTIFY_SECRET),
        "open_sessions": open_sessions,
    }
    sha = live_git_sha()
    if sha:
        body["git_sha"] = sha
    if _catalog_cleanup_last is not None:
        body["catalog_cleanup"] = dict(_catalog_cleanup_last)
    try:
        import db as _db
        import unicorn_shop

        shop = unicorn_shop.find_catalog_shop()
        if shop:
            import vendor_stores as _vs

            rows = _db.list_payment_methods(int(shop["chat_id"]), active_only=False)
            active = sum(1 for m in rows if m.get("active"))
            usable = sum(
                1 for m in rows if m.get("active") and _vs.payment_rail_usable(m)
            )
            body["payments"] = {
                "active": active,
                "total": len(rows),
                "usable": usable,
                "checkout_ready": usable > 0,
            }
    except Exception:
        pass
    try:
        import vendor_stores

        body["store_url_cache_bust"] = vendor_stores.STORE_URL_CACHE_BUST
    except Exception:
        pass
    try:
        import tg_payments

        body["invoices"] = {"enabled": bool(tg_payments.invoices_enabled())}
    except Exception:
        body["invoices"] = {"enabled": False}
    body["notify"] = notify_stats()
    try:
        import unicorn_shop as _ushop
        import vendor_stores as _vstores

        cat = _ushop.find_catalog_shop()
        recipient_n = 0
        if cat:
            sid = int(cat["chat_id"])
            recipient_n = len(
                _vstores.build_notify_recipient_ids(
                    _vstores.base_notify_ids_for_shop(sid), sid
                )
            )
        body["notify"]["recipients_configured"] = recipient_n > 0
        body["notify"]["recipient_count"] = recipient_n
        recent = _ushop.recent_orders_snapshot(20)
        body["orders"] = {
            "recent": [
                {
                    "id": r["id"],
                    "payment_code": r["payment_code"],
                    "chat_id": r["chat_id"],
                    "status": r["status"],
                    "username": r["username"],
                }
                for r in recent
            ]
        }
        if _keep_only_last is not None:
            body["orders"]["keep_only"] = dict(_keep_only_last)
    except Exception:
        body["notify"]["recipients_configured"] = False
        body["notify"]["recipient_count"] = 0
    return body


def _miniapp_invite_and_init(payload: dict) -> tuple[str, str]:
    invite = str(payload.get("invite") or "").strip()
    init_data = payload.get("initData")
    if init_data is not None and not isinstance(init_data, str):
        init_data = str(init_data)
    return invite, (init_data or "").strip()


def _auth_miniapp_buyer(
    invite: str, init_data: str, *, log_label: str
) -> tuple[int | None, dict]:
    """Storefront key + initData. Fail: (status, error_body). Ok: (None, ctx)."""
    import vendor_stores
    import webpanel

    shop_chat_id = webpanel.resolve_storefront_key(invite)
    if shop_chat_id is None:
        log.info("%s unknown storefront invite", log_label)
        return 404, vendor_stores.checkout_error_body("unknown storefront")

    vendor_tokens = list(vendor_stores.get_bot_tokens_for_shop(shop_chat_id) or [])
    one = vendor_stores.get_bot_token_for_shop(shop_chat_id)
    if one and one not in vendor_tokens:
        vendor_tokens.insert(0, one)
    if not vendor_tokens:
        log.warning("%s no vendor bot token for shop=%s", log_label, shop_chat_id)
        return 401, vendor_stores.checkout_error_body("no_vendor_token")
    vendor_token = vendor_tokens[0]
    try:
        buyer = vendor_stores.validate_webapp_init_data_any(
            init_data, vendor_tokens
        )
    except vendor_stores.InitDataError as exc:
        err = vendor_stores.initdata_error_code(exc)
        log.info(
            "%s initData rejected shop=%s reason=%s error=%s",
            log_label,
            shop_chat_id,
            getattr(exc, "reason", None) or exc,
            err,
        )
        return 401, vendor_stores.checkout_error_body(
            err,
            detail=str(getattr(exc, "reason", None) or err)[:80],
        )
    return None, {
        "shop_chat_id": int(shop_chat_id),
        "buyer": buyer,
        "vendor_token": vendor_token,
    }


def handle_http_order(payload: dict) -> tuple[int, dict]:
    """POST /order — mini-app checkout for all launch modes (initData auth).

    Sync only (HTTP thread). One successful call creates exactly one order.
    Never raises — maps failures to 400/401/404/409 JSON.
    """
    import db
    import vendor_stores
    import webpanel

    try:
        if not isinstance(payload, dict):
            return 400, vendor_stores.checkout_error_body("bad payload")

        invite, init_data = _miniapp_invite_and_init(payload)
        raw_items = payload.get("items")

        log.info(
            "POST /order invite=%s initData_len=%s items=%s",
            (invite[:8] + "…") if len(invite) > 8 else invite,
            len(init_data),
            len(raw_items) if isinstance(raw_items, list) else type(raw_items).__name__,
        )

        err_status, auth = _auth_miniapp_buyer(
            invite, init_data, log_label="POST /order"
        )
        if err_status is not None:
            return err_status, auth
        shop_chat_id = int(auth["shop_chat_id"])
        buyer = auth["buyer"]
        vendor_token = auth["vendor_token"]

        buyer_id = int(buyer["user_id"])
        username = buyer.get("username")
        full_name = buyer.get("full_name")
        log.info(
            "POST /order auth ok shop=%s buyer=%s (@%s)",
            shop_chat_id,
            buyer_id,
            username or "—",
        )

        # 3) Cart + ship (same helpers as on_web_app_data)
        try:
            items = vendor_stores.parse_miniapp_cart_items(raw_items)
        except Exception as exc:
            log.info("POST /order bad cart shop=%s: %s", shop_chat_id, exc)
            return 400, vendor_stores.checkout_error_body("bad payload")
        if not items:
            return 400, vendor_stores.checkout_error_body("empty cart")

        if not vendor_stores.shop_checkout_ready(shop_chat_id):
            log.info("POST /order no usable payment methods shop=%s", shop_chat_id)
            return 409, vendor_stores.checkout_error_body("no_payment_methods")

        shop_row = db.get_shop(shop_chat_id) or db.ensure_shop(shop_chat_id)
        ok_min, _min_msg = db.check_min_order(
            shop_row, db.cart_quantity_total(items)
        )
        if not ok_min:
            log.info("POST /order below min_order shop=%s", shop_chat_id)
            return 409, vendor_stores.checkout_error_body("min_order")

        ship_name, ship_address, ship_notes = vendor_stores.parse_ship_fields(
            payload
        )

        # 4) Create order (exactly once)
        order = db.create_order(
            chat_id=shop_chat_id,
            user_id=buyer_id,
            username=username,
            full_name=full_name,
            items=items,
            payment_method=None,
            ship_name=ship_name,
            ship_address=ship_address,
            ship_notes=ship_notes,
        )
        if not order:
            log.info(
                "POST /order stock/create failed shop=%s buyer=%s",
                shop_chat_id,
                buyer_id,
            )
            return 409, vendor_stores.checkout_error_body("sold_out")

        order_id = int(order["id"])
        code = order.get("payment_code") or f"#{order_id}"
        total = float(order.get("total") or 0)
        log.info(
            "POST /order created order_id=%s code=%s total=%s shop=%s",
            order_id,
            code,
            total,
            shop_chat_id,
        )

        meta = vendor_stores.vendor_meta_for_shop(shop_chat_id)
        emoji = meta.get("emoji") or "\U0001f6cd"
        shop_name = meta.get("name") or "the shop"
        order_lines = db.get_order_items(order_id)

        # 5) Notify owner/vendor (sync; vendor bot then main bot)
        note = vendor_stores.build_new_order_notify_text(
            order,
            shop_name=shop_name,
            emoji=emoji,
            order_lines=order_lines,
        )
        _notify_staff(
            shop_chat_id,
            vendor_token,
            note,
            order_id=order_id,
            kind="order",
            log_label="POST /order",
        )

        # 6) Buyer confirmation — HTML (tap-to-copy code + pay links),
        #    plain-text fallback.
        message = vendor_stores.build_customer_order_received_text(
            order,
            shop_chat_id,
            emoji=emoji,
            markdown=False,
            order_lines=order_lines,
        )
        html_message = vendor_stores.build_customer_order_received_html(
            order,
            shop_chat_id,
            emoji=emoji,
            order_lines=order_lines,
        )
        try:
            ok_buyer = webpanel.telegram_send_with_token(
                vendor_token, buyer_id, html_message, parse_mode="HTML"
            )
            if not ok_buyer:
                ok_buyer = webpanel.telegram_send_with_token(
                    vendor_token, buyer_id, message, parse_mode=None
                )
            if not ok_buyer:
                log.warning(
                    "POST /order buyer confirm failed shop=%s buyer=%s",
                    shop_chat_id,
                    buyer_id,
                )
        except Exception:
            log.exception(
                "POST /order buyer confirm error shop=%s buyer=%s",
                shop_chat_id,
                buyer_id,
            )

        pay_objs = vendor_stores.payment_methods_public(
            shop_chat_id, total, code
        )
        payments = [p["line"] for p in pay_objs]
        invoice_sent = False
        try:
            import tg_payments

            invoice_sent = tg_payments.send_invoice_for_order(
                order, shop_chat_id, buyer_id, bot_token=vendor_token
            )
        except Exception:
            log.exception(
                "POST /order telegram invoice send failed shop=%s order=%s",
                shop_chat_id,
                order_id,
            )
        return 200, {
            "ok": True,
            "code": code,
            "total": total,
            "needs_payment": True,
            "can_mark_paid": True,
            "mark_paid_hint": vendor_stores.mark_paid_buyer_hint("pending_payment"),
            "payments": payments,
            "payment_methods": pay_objs,
            "message": message,
            "checkout_message": vendor_stores.order_status_buyer_message(
                "pending_payment", needs_payment=True
            ),
            "invoice_offered": bool(invoice_sent),
            "invoices_enabled": vendor_stores.public_invoices_enabled(),
        }
    except Exception as exc:
        log.error("POST /order unexpected error: %s", exc, exc_info=exc)
        return 400, vendor_stores.checkout_error_body("bad payload")


def _notify_vendor_payment_claim(
    shop_chat_id: int, vendor_token: str, order: dict
) -> None:
    """Plain-text ping (HTTP thread). No stock change. Never raises.

    Confirm is a URL button (/confirm?ct=) so it works on the vendor bot,
    which does not handle admconfirm callbacks.
    Vendor token first, then TELEGRAM_BOT_TOKEN / OWNER. Empty recipients escalate.
    """
    import vendor_stores

    note = vendor_stores.build_payment_claim_notify_text(order)
    markup = vendor_stores.payment_claim_reply_markup(order)
    _notify_staff(
        shop_chat_id,
        vendor_token,
        note,
        order_id=order.get("id"),
        kind="claim",
        log_label="POST /order-paid",
        reply_markup=markup,
    )


def _notify_buyer_payment_claim(
    vendor_token: str, buyer_id: int, order: dict
) -> None:
    """Vendor-bot DM to the buyer after I've paid. Never raises. No stock change."""
    import vendor_stores
    import webpanel

    if not buyer_id:
        return
    note = vendor_stores.build_payment_claim_buyer_text(order)
    try:
        webpanel.telegram_send_with_token(
            vendor_token, buyer_id, note, parse_mode=None
        )
    except Exception:
        log.exception(
            "POST /order-paid buyer claim DM failed buyer=%s order=%s",
            buyer_id,
            order.get("id"),
        )


def handle_http_order_paid(payload: dict) -> tuple[int, dict]:
    """POST /order-paid — Mini App buyer taps I've paid (initData auth).

    pending_payment → awaiting_confirmation. Idempotent if already awaiting.
    Never confirms payment, never changes stock. Never raises.
    """
    import db
    import vendor_stores
    import webpanel

    try:
        if not isinstance(payload, dict):
            return 400, vendor_stores.checkout_error_body("bad payload")

        invite, init_data = _miniapp_invite_and_init(payload)
        code = str(payload.get("code") or "").strip()
        log.info(
            "POST /order-paid invite=%s initData_len=%s code_len=%s",
            (invite[:8] + "…") if len(invite) > 8 else invite,
            len(init_data),
            len(code),
        )
        if not code:
            return 400, vendor_stores.checkout_error_body("missing_code")

        err_status, auth = _auth_miniapp_buyer(
            invite, init_data, log_label="POST /order-paid"
        )
        if err_status is not None:
            return err_status, auth
        shop_chat_id = int(auth["shop_chat_id"])
        buyer = auth["buyer"]
        vendor_token = auth["vendor_token"]
        buyer_id = int(buyer["user_id"])

        order = db.get_order_by_payment_code(code)
        if not order or int(order.get("chat_id") or 0) != shop_chat_id:
            return 404, vendor_stores.checkout_error_body("order not found")
        if int(order.get("user_id") or 0) != buyer_id:
            log.info(
                "POST /order-paid not_your_order shop=%s buyer=%s order=%s",
                shop_chat_id,
                buyer_id,
                order.get("id"),
            )
            return 403, vendor_stores.checkout_error_body("not_your_order")

        status = (order.get("status") or "").strip().lower()
        newly_claimed = False
        if status == "pending_payment":
            ok = db.mark_order_awaiting_confirmation(int(order["id"]))
            if not ok:
                return 409, vendor_stores.checkout_error_body("already_processed")
            newly_claimed = True
            order = db.get_order(int(order["id"])) or order
            _notify_vendor_payment_claim(shop_chat_id, vendor_token, order)
            _notify_buyer_payment_claim(vendor_token, buyer_id, order)
            log.info(
                "POST /order-paid claimed order_id=%s code=%s shop=%s",
                order.get("id"),
                code,
                shop_chat_id,
            )
        elif status != "awaiting_confirmation":
            return 409, vendor_stores.checkout_error_body("already_processed")

        st, body = webpanel.api_order_status(invite, code)
        if st != 200:
            return st, body
        body["ok"] = True
        body["claimed"] = True
        body["newly_claimed"] = newly_claimed
        return 200, body
    except Exception as exc:
        log.error("POST /order-paid unexpected error: %s", exc, exc_info=exc)
        return 400, vendor_stores.checkout_error_body("bad payload")


class NotifyHTTPHandler(BaseHTTPRequestHandler):
    server_version = "SPBCNotify/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _cors_order_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, body: dict, *, cors: bool = False) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if cors:
            self._cors_order_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _check_secret(self) -> bool:
        if not NOTIFY_SECRET:
            self._json(
                503,
                {
                    "error": "misconfigured",
                    "message": "NOTIFY_SECRET not set on this service",
                },
            )
            return False
        header = self.headers.get("X-Notify-Secret") or ""
        if not header:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                header = auth[7:].strip()
        # Constant-time compare (length mismatch still fails closed)
        try:
            ok = hmac.compare_digest(str(header), str(NOTIFY_SECRET))
        except Exception:
            ok = False
        if not ok:
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _read_json(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _send_raw(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if content_type.startswith("text/html"):
            self.send_header("X-Robots-Tag", "noindex")
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/order", "/order-paid"):
            self.send_response(204)
            self._cors_order_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/order", "/order-paid"):
            # CORS-visible method probe; checkout / I've paid are POST only.
            self._json(
                405,
                {"ok": False, "error": "method not allowed"},
                cors=True,
            )
            return
        if path in ("/", "/health"):
            self._json(200, _status_body())
            return
        if path.startswith("/media/"):
            # Public by necessity: Telegram fetches product photos by URL.
            # Names are 128-bit random and regex-validated before any file open.
            import webpanel

            got = webpanel.read_media(path[len("/media/") :])
            if got is None:
                self._json(404, {"error": "not_found"})
                return
            blob, ctype = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            # Never let a stored file be interpreted as markup
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("X-Robots-Tag", "noindex")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            if ctype == "application/pdf":
                # download rather than render: a PDF can carry active content
                self.send_header("Content-Disposition", "attachment")
            self.end_headers()
            self.wfile.write(blob)
            return
        if path == "/storefront":
            # Public read-only catalog for vendor mini-app stores (CORS: the
            # store is served from a different origin, e.g. *.pages.dev).
            # ?invite= is the public storefront_key (NOT the claim token).
            import webpanel

            query = urllib.parse.parse_qs(parsed.query)
            invite = (query.get("invite") or [""])[0]
            code, body_obj = webpanel.api_storefront(invite)
            raw = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/order-status":
            # Read-only buyer lookup. Same storefront_keys scope as /storefront.
            # vendor_invites never resolve. No write/confirm/claim.
            import webpanel

            query = urllib.parse.parse_qs(parsed.query)
            invite = (query.get("invite") or [""])[0]
            code = (query.get("code") or [""])[0]
            status, body_obj = webpanel.api_order_status(invite, code)
            raw = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/panel/api/orders.txt":
            # Plain-text order history download (attachment). Token required.
            import webpanel

            query = urllib.parse.parse_qs(parsed.query)
            tok = webpanel.resolve_token((query.get("t") or [""])[0])
            if not tok:
                self._json(401, {"ok": False, "error": "invalid_or_expired_link"})
                return
            start = (query.get("start") or [""])[0]
            end = (query.get("end") or [""])[0]
            text, filename = webpanel.api_order_history_txt(tok, start, end)
            raw = text.encode("utf-8")
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "orders.txt"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header(
                "Content-Disposition", f'attachment; filename="{safe_name}"'
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/track":
            # Narrow order-action token auth (ot=). Not the admin panel.
            import webpanel

            query = urllib.parse.parse_qs(parsed.query)
            code, ctype, body = webpanel.handle_track_get(query)
            self._send_raw(code, ctype, body)
            return
        if path == "/confirm":
            # Narrow order-action token auth (ct=). Confirm payment only.
            import webpanel

            query = urllib.parse.parse_qs(parsed.query)
            code, ctype, body = webpanel.handle_confirm_get(query)
            self._send_raw(code, ctype, body)
            return
        if path == "/cancel":
            # Narrow order-action token auth (xt=). Cancel order only (two-step).
            import webpanel

            query = urllib.parse.parse_qs(parsed.query)
            code, ctype, body = webpanel.handle_cancel_get(query)
            self._send_raw(code, ctype, body)
            return
        if path.startswith("/panel"):
            import webpanel

            query = urllib.parse.parse_qs(parsed.query)
            code, ctype, body = webpanel.handle_panel_get(path, query)
            self._send_raw(code, ctype, body)
            return
        if path == "/recent-chats":
            if not self._check_secret():
                return
            self._json(200, {"ok": True, "chats": recent_chats()})
            return
        if path == "/vendor-catalogs":
            # SPBC admin product↔vendor matching UI
            if not self._check_secret():
                return
            try:
                import vendor_links

                from config import SPBC_SHOP_CHAT_ID as _own

                self._json(
                    200,
                    {
                        "ok": True,
                        "vendors": vendor_links.vendor_catalogs(
                            exclude_shop_id=_own or None
                        ),
                    },
                )
            except Exception as exc:
                log.error("vendor-catalogs failed: %s", exc, exc_info=exc)
                self._json(502, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"error": "not_found"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def _read_form_or_json(self) -> tuple[Optional[dict], bool]:
        """Parse POST body as JSON or form-urlencoded. Returns (dict|None, wants_json)."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return None, ctype == "application/json"
        try:
            raw = self.rfile.read(length)
        except Exception:
            return None, ctype == "application/json"
        if ctype == "application/json":
            try:
                data = json.loads(raw.decode("utf-8"))
                return (data if isinstance(data, dict) else None), True
            except Exception:
                return None, True
        # form (or default): application/x-www-form-urlencoded
        try:
            qs = urllib.parse.parse_qs(
                raw.decode("utf-8"), keep_blank_values=True
            )
            return {k: (v[0] if v else "") for k, v in qs.items()}, False
        except Exception:
            return None, False

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/order":
            try:
                payload = self._read_json()
                if payload is None:
                    self._json(
                        400, {"ok": False, "error": "bad payload"}, cors=True
                    )
                    return
                code, body = handle_http_order(payload)
                self._json(code, body, cors=True)
            except Exception as exc:
                log.error("POST /order handler crash: %s", exc, exc_info=exc)
                try:
                    self._json(
                        400, {"ok": False, "error": "bad payload"}, cors=True
                    )
                except Exception:
                    pass
            return
        if path == "/order-paid":
            try:
                payload = self._read_json()
                if payload is None:
                    self._json(
                        400, {"ok": False, "error": "bad payload"}, cors=True
                    )
                    return
                code, body = handle_http_order_paid(payload)
                self._json(code, body, cors=True)
            except Exception as exc:
                log.error("POST /order-paid handler crash: %s", exc, exc_info=exc)
                try:
                    self._json(
                        400, {"ok": False, "error": "bad payload"}, cors=True
                    )
                except Exception:
                    pass
            return
        if path == "/track":
            import webpanel

            payload, wants_json = self._read_form_or_json()
            if payload is None:
                if wants_json:
                    self._json(400, {"ok": False, "error": "bad_body"})
                else:
                    self._send_raw(
                        400,
                        "text/html; charset=utf-8",
                        b"<!doctype html><html><body><p>Bad request.</p></body></html>",
                    )
                return
            code, ctype, body = webpanel.handle_track_post(
                payload, wants_json=wants_json
            )
            self._send_raw(code, ctype, body)
            return
        if path == "/confirm":
            import webpanel

            payload, wants_json = self._read_form_or_json()
            if payload is None:
                if wants_json:
                    self._json(400, {"ok": False, "error": "bad_body"})
                else:
                    self._send_raw(
                        400,
                        "text/html; charset=utf-8",
                        b"<!doctype html><html><body><p>Bad request.</p></body></html>",
                    )
                return
            code, ctype, body = webpanel.handle_confirm_post(
                payload, wants_json=wants_json
            )
            self._send_raw(code, ctype, body)
            return
        if path == "/cancel":
            import webpanel

            payload, wants_json = self._read_form_or_json()
            if payload is None:
                if wants_json:
                    self._json(400, {"ok": False, "error": "bad_body"})
                else:
                    self._send_raw(
                        400,
                        "text/html; charset=utf-8",
                        b"<!doctype html><html><body><p>Bad request.</p></body></html>",
                    )
                return
            code, ctype, body = webpanel.handle_cancel_post(
                payload, wants_json=wants_json
            )
            self._send_raw(code, ctype, body)
            return
        if path.startswith("/panel/api/"):
            import webpanel

            payload = self._read_json()
            if payload is None:
                self._json(400, {"error": "bad_json"})
                return
            code, ctype, body = webpanel.handle_panel_post(path, payload)
            self._send_raw(code, ctype, body)
            return
        if path == "/vendor-create":
            # SPBC admin: stand up a vendor shop and get their handover link,
            # so a vendor can be added without opening Telegram.
            if not self._check_secret():
                return
            payload = self._read_json()
            if payload is None:
                self._json(400, {"error": "bad_json"})
                return
            try:
                import db as _db
                import webpanel as _wp
                from config import OWNER_IDS as _owners

                name = " ".join(str(payload.get("name") or "").split())[:80]
                if not name:
                    self._json(400, {"ok": False, "message": "Vendor name required"})
                    return
                owner = min(_owners) if _owners else 0
                shop = _db.create_virtual_shop(name, owner)
                sid = int(shop["chat_id"])
                token = _wp.create_vendor_invite(owner, name, shop_chat_id=sid)
                me = _telegram_api("getMe", {})
                username = ((me.get("result") or {}).get("username") or "").lstrip("@")
                link = (
                    f"https://t.me/{username}?start=vendor{token}" if username else ""
                )
                panel = ""
                try:
                    from config import PANEL_BASE_URL as _panel

                    if _panel:
                        panel = _wp.panel_url(_panel, _wp.issue_token(sid, owner))
                except Exception:
                    panel = ""
                log.info("vendor shop created via admin: %s (%s)", name, sid)
                self._json(
                    200,
                    {
                        "ok": True,
                        "shop_chat_id": sid,
                        "title": shop["title"],
                        "handover_link": link,
                        "panel_link": panel,
                    },
                )
            except Exception as exc:
                log.error("vendor-create failed: %s", exc, exc_info=exc)
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if path == "/vendor-link":
            if not self._check_secret():
                return
            payload = self._read_json()
            if payload is None:
                self._json(400, {"error": "bad_json"})
                return
            try:
                import vendor_links

                name = str(payload.get("spbc_name") or "")
                shop = int(payload.get("shop_chat_id") or 0)
                if payload.get("clear"):
                    ok = vendor_links.clear_link(name, shop)
                    self._json(200, {"ok": True, "cleared": ok})
                    return
                ok, msg = vendor_links.set_link(
                    name, shop, int(payload.get("vendor_product_id") or 0)
                )
                self._json(200 if ok else 400, {"ok": ok, "message": msg})
            except Exception as exc:
                log.error("vendor-link failed: %s", exc, exc_info=exc)
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if path not in ("/notify", "/resolve-chat"):
            self._json(404, {"error": "not_found"})
            return
        if not self._check_secret():
            return
        payload = self._read_json()
        if payload is None:
            self._json(400, {"error": "bad_json"})
            return
        try:
            if path == "/notify":
                code, body = handle_notify(payload)
            else:
                code, body = handle_resolve_chat(payload)
        except NotifyError as exc:
            code = 503 if exc.code == "misconfigured" else 502
            body = {"error": exc.code, "message": str(exc)}
        except Exception as exc:
            log.error("notify_http_error path=%s: %s", path, exc, exc_info=exc)
            code, body = 502, {"error": "send_failed", "message": str(exc)}
        self._json(code, body)


def serve_http(port: int) -> None:
    """Blocking; run on a daemon thread (see run_cloud.py)."""
    server = ThreadingHTTPServer(("0.0.0.0", port), NotifyHTTPHandler)
    log.info("SPBC notify HTTP listening on :%s", port)
    server.serve_forever()
