"""Owner-side bridge to the spbc-orders worker admin API.

Used when the Gmail payment-receipt matcher misses a payment: the owner can
confirm it straight from Telegram instead of opening the web admin. Marking a
franchisee's wholesale invoice paid is what releases the order to vendors, so
this is deliberately owner-only and always reports what actually happened.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from config import SPBC_ORDERS_ADMIN_TOKEN, SPBC_ORDERS_URL

log = logging.getLogger("orders_admin")

TIMEOUT_SEC = 25


def configured() -> bool:
    return bool(SPBC_ORDERS_URL and SPBC_ORDERS_ADMIN_TOKEN)


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    url = f"{SPBC_ORDERS_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {SPBC_ORDERS_ADMIN_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
            raw = res.read().decode("utf-8")
            return res.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        return 0, {"error": str(exc)}


def get_order(order_number: str) -> Optional[dict]:
    code, body = _request(
        "GET", f"/admin/orders/{urllib.parse.quote(order_number.strip().upper())}"
    )
    if code != 200 or not isinstance(body, dict):
        return None
    return body.get("order") or body


def mark_paid(order_number: str, note: str = "") -> tuple[bool, str, Optional[dict]]:
    """Transition an order to paid. Returns (ok, message, order)."""
    num = (order_number or "").strip().upper()
    if not num:
        return False, "Order number required.", None
    if not configured():
        return (
            False,
            "Not configured — set SPBC_ORDERS_ADMIN_TOKEN on this service.",
            None,
        )
    payload: dict[str, Any] = {"status": "paid"}
    if note:
        payload["note"] = note[:200]
    code, body = _request(
        "PATCH", f"/admin/orders/{urllib.parse.quote(num)}", payload
    )
    if code == 200:
        order = body.get("order") if isinstance(body, dict) else None
        log.info("marked paid via telegram order=%s", num)
        return True, "Marked paid.", order
    if code == 401 or code == 403:
        return False, "Admin token rejected by spbc-orders.", None
    if code == 404:
        return False, f"Order {num} not found.", None
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("error") or "")
    return False, msg or f"Failed (HTTP {code}).", None


def set_tracking(
    order_number: str, tracking: str, carrier: str = "", notify: bool = True
) -> tuple[bool, str, Optional[dict]]:
    """Attach tracking to a website order and (optionally) email the customer.

    Website buyers have no Telegram — the worker's shipped transition is what
    sends them their email, so `notify` controls whether that goes out.
    """
    num = (order_number or "").strip().upper()
    tn = (tracking or "").strip()
    if not num or not tn:
        return False, "Order number and tracking required.", None
    if not configured():
        return (
            False,
            "Not configured — set SPBC_ORDERS_ADMIN_TOKEN on this service.",
            None,
        )
    payload: dict[str, Any] = {
        "status": "shipped",
        "tracking_number": tn,
        "send_email": bool(notify),
    }
    if carrier.strip():
        payload["tracking_carrier"] = carrier.strip()[:60]
    code, body = _request("PATCH", f"/admin/orders/{urllib.parse.quote(num)}", payload)
    if code == 200:
        order = body.get("order") if isinstance(body, dict) else None
        emailed = bool(
            isinstance(body, dict) and (body.get("email") or {}).get("sent")
        )
        log.info("tracking set via telegram order=%s emailed=%s", num, emailed)
        return (
            True,
            "Tracking saved and customer emailed." if emailed else "Tracking saved.",
            order,
        )
    if code in (401, 403):
        return False, "Admin token rejected by spbc-orders.", None
    if code == 404:
        return False, f"Order {num} not found.", None
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("error") or "")
    return False, msg or f"Failed (HTTP {code}).", None
