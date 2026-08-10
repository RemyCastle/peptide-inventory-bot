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
        lines += ["", "Ship to:"]
        city_line = ", ".join(
            str(v) for v in (ship.get("city"), ship.get("state"), ship.get("postal")) if v
        )
        for s in (
            ship.get("name"),
            ship.get("line1"),
            ship.get("line2"),
            city_line,
            ship.get("country"),
            ship.get("phone"),
        ):
            if s:
                lines.append(str(s))
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


# ── /notify core (called by the HTTP handler) ────────────────────────────────

def handle_notify(payload: dict) -> tuple[int, dict]:
    status = str(payload.get("status") or "").lower()
    order_number = payload.get("order_number") or payload.get("orderNumber")
    if not order_number:
        return 400, {"error": "order_number required"}

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


def _status_body() -> dict:
    with _state_lock:
        token_ok = bool(_bot_token)
        open_sessions = len(_sessions)
    return {
        "service": "spbc-supplier-bot",
        "ok": True,
        "mode": "combined_inventory_bot",
        "telegram_bot_configured": token_ok,
        "telegram_configured": token_ok,
        "telegram_bot_ok": token_ok,
        "default_chat_configured": bool(SUPPLIER_TELEGRAM_CHAT_ID),
        "owner_chat_configured": bool(OWNER_TELEGRAM_CHAT_ID or SUPPLIER_TELEGRAM_CHAT_ID),
        "notify_secret_configured": bool(NOTIFY_SECRET),
        "open_sessions": open_sessions,
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
            return 400, {"ok": False, "error": "bad payload"}

        invite = str(payload.get("invite") or "").strip()
        init_data = payload.get("initData")
        if init_data is not None and not isinstance(init_data, str):
            init_data = str(init_data)
        init_data = (init_data or "").strip()
        raw_items = payload.get("items")

        log.info(
            "POST /order invite=%s initData_len=%s items=%s",
            (invite[:8] + "…") if len(invite) > 8 else invite,
            len(init_data),
            len(raw_items) if isinstance(raw_items, list) else type(raw_items).__name__,
        )

        # 1) Resolve shop via public storefront_keys only
        shop_chat_id = webpanel.resolve_storefront_key(invite)
        if shop_chat_id is None:
            log.info("POST /order unknown storefront invite")
            return 404, {"ok": False, "error": "unknown storefront"}

        # 2) Auth boundary: initData signed with this shop's vendor bot token
        vendor_token = vendor_stores.get_bot_token_for_shop(shop_chat_id)
        if not vendor_token:
            log.warning(
                "POST /order no vendor bot token for shop=%s", shop_chat_id
            )
            return 401, {"ok": False, "error": "invalid initData"}
        try:
            buyer = vendor_stores.validate_webapp_init_data(init_data, vendor_token)
        except vendor_stores.InitDataError as exc:
            log.info(
                "POST /order initData rejected shop=%s: %s", shop_chat_id, exc
            )
            return 401, {"ok": False, "error": "invalid initData"}

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
            return 400, {"ok": False, "error": "bad payload"}
        if not items:
            return 400, {"ok": False, "error": "empty cart"}

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
            return 409, {"ok": False, "error": ORDER_STOCK_ERROR}

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
        base_ids = vendor_stores.base_notify_ids_for_shop(shop_chat_id)
        recipients = vendor_stores.build_notify_recipient_ids(base_ids, shop_chat_id)
        for rid in recipients:
            delivered = False
            try:
                delivered = bool(
                    webpanel.telegram_send_with_token(
                        vendor_token, rid, note, parse_mode=None
                    )
                )
            except Exception:
                log.exception(
                    "POST /order vendor notify failed shop=%s rid=%s",
                    shop_chat_id,
                    rid,
                )
            if delivered:
                log.info(
                    "POST /order notified rid=%s via vendor bot", rid
                )
                continue
            try:
                send_telegram(rid, note)
                log.info("POST /order notified rid=%s via main bot", rid)
            except Exception as exc:
                log.warning(
                    "POST /order notify failed rid=%s: %s", rid, exc
                )

        # 6) Buyer confirmation (same body as sendData path, plain text)
        message = vendor_stores.build_customer_order_received_text(
            order,
            shop_chat_id,
            emoji=emoji,
            markdown=False,
            order_lines=order_lines,
        )
        try:
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

        payments = vendor_stores.payment_display_lines(shop_chat_id)
        return 200, {
            "ok": True,
            "code": code,
            "total": total,
            "payments": payments,
            "message": message,
        }
    except Exception as exc:
        log.error("POST /order unexpected error: %s", exc, exc_info=exc)
        return 400, {"ok": False, "error": "bad payload"}


class NotifyHTTPHandler(BaseHTTPRequestHandler):
    server_version = "SPBCNotify/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _cors_order_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, body: dict, *, cors: bool = False) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
        if path == "/order":
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
        if path == "/order":
            # CORS-visible method probe; checkout is POST only.
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
            raw = json.dumps(body_obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
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
