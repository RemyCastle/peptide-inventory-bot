"""Optional Telegram Payments for physical goods.

Additive only. Invoices are sent only when TELEGRAM_PAYMENT_PROVIDER_TOKEN
is set. Empty token = handlers exist but send is a no-op. Never uses
Telegram Stars (XTR / empty provider). Never logs the token.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

import db
from config import CURRENCY, TELEGRAM_PAYMENT_PROVIDER_TOKEN

log = logging.getLogger("tg_payments")

INVOICE_PAYLOAD_PREFIX = "umf-order:"


def payment_provider_token() -> str:
    return (TELEGRAM_PAYMENT_PROVIDER_TOKEN or "").strip()


def invoices_enabled() -> bool:
    return bool(payment_provider_token())


def invoice_currency(shop: dict | None = None) -> str:
    """Fiat currency for physical goods. Stars (XTR) are never used."""
    raw = ((shop or {}).get("currency") or CURRENCY or "USD").strip().upper()
    if raw == "XTR" or not raw:
        return "USD"
    return raw[:3]


def invoice_payload(order_id: int) -> str:
    return f"{INVOICE_PAYLOAD_PREFIX}{int(order_id)}"


def parse_invoice_payload(payload: str) -> int | None:
    raw = (payload or "").strip()
    if not raw.startswith(INVOICE_PAYLOAD_PREFIX):
        return None
    rest = raw[len(INVOICE_PAYLOAD_PREFIX) :]
    try:
        oid = int(rest)
    except (TypeError, ValueError):
        return None
    return oid if oid > 0 else None


def amount_cents(value: Any) -> int:
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError):
        return 0


def build_invoice_body(
    order: dict,
    *,
    chat_id: int,
    items: list[dict] | None = None,
    shop: dict | None = None,
) -> dict | None:
    """Telegram sendInvoice body, or None when invoices are disabled."""
    if not invoices_enabled():
        return None
    token = payment_provider_token()
    if not token:
        return None
    oid = int(order["id"])
    shop = shop or db.get_shop(int(order.get("chat_id") or 0)) or {}
    currency = invoice_currency(shop)
    if currency == "XTR":
        return None
    title = f"Order {order.get('payment_code') or ('#' + str(oid))}"
    title = title[:32]
    lines = items if items is not None else db.get_order_items(oid)
    desc_parts = [
        f"{it.get('product_name') or 'Item'} × {int(it.get('quantity') or 0)}"
        for it in (lines or [])[:6]
    ]
    description = "; ".join(desc_parts) or "Shop order"
    prices = [
        {
            "label": "Order",
            "amount": amount_cents(order.get("total")),
        }
    ]
    if prices[0]["amount"] <= 0:
        return None
    return {
        "chat_id": int(chat_id),
        "title": title,
        "description": description[:255],
        "payload": invoice_payload(oid),
        "provider_token": token,
        "currency": currency,
        "prices": prices,
        "need_name": False,
        "need_phone_number": False,
        "need_email": False,
        "need_shipping_address": False,
        "is_flexible": False,
    }


def send_invoice_via_token(bot_token: str, body: dict) -> bool:
    """POST sendInvoice. Caller must not log provider_token."""
    bot = (bot_token or "").strip()
    if not bot or not body:
        return False
    url = f"https://api.telegram.org/bot{bot}/sendInvoice"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = json.loads(res.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except urllib.error.HTTPError as exc:
        log.warning("sendInvoice HTTP %s", exc.code)
        return False
    except Exception as exc:
        log.warning("sendInvoice failed: %s", type(exc).__name__)
        return False


def send_invoice_for_order(
    order: dict,
    shop_chat_id: int,
    buyer_id: int,
    *,
    bot_token: str,
) -> bool:
    """Offer a Telegram invoice when a provider token is configured."""
    if not invoices_enabled():
        return False
    shop = db.get_shop(int(shop_chat_id)) or {}
    items = db.get_order_items(int(order["id"]))
    body = build_invoice_body(
        order, chat_id=int(buyer_id), items=items, shop=shop
    )
    if not body:
        return False
    return send_invoice_via_token(bot_token, body)


def validate_pre_checkout(
    payload: str,
    currency: str,
    total_amount: int,
) -> tuple[bool, str]:
    """Server-authoritative pre-checkout. Never confirms or deducts stock."""
    if (currency or "").strip().upper() == "XTR":
        return False, "Stars are not accepted for physical goods."
    if not invoices_enabled():
        return False, "Card checkout is not enabled."
    oid = parse_invoice_payload(payload)
    if oid is None:
        return False, "Unknown invoice."
    order = db.get_order(oid)
    if not order:
        return False, "Order not found."
    if order.get("status") in ("cancelled", "rejected"):
        return False, "This order is no longer open."
    if order.get("status") == "paid":
        return False, "This order is already paid."
    shop = db.get_shop(int(order.get("chat_id") or 0)) or {}
    want_cur = invoice_currency(shop)
    if (currency or "").strip().upper() != want_cur:
        return False, "Currency mismatch."
    if int(total_amount) != amount_cents(order.get("total")):
        return False, "Amount mismatch."
    return True, ""


def apply_successful_payment(
    payload: str, telegram_charge_id: str
) -> tuple[bool, str]:
    """Record the Telegram charge. Admin confirm still deducts stock."""
    oid = parse_invoice_payload(payload)
    if oid is None:
        return False, "Unknown invoice."
    return db.record_telegram_payment_charge(oid, telegram_charge_id)


async def on_pre_checkout_query(update, context) -> None:  # noqa: ANN001
    query = update.pre_checkout_query
    if query is None:
        return
    ok, err = validate_pre_checkout(
        getattr(query, "invoice_payload", "") or "",
        getattr(query, "currency", "") or "",
        int(getattr(query, "total_amount", 0) or 0),
    )
    try:
        if ok:
            await query.answer(ok=True)
        else:
            await query.answer(ok=False, error_message=(err or "Payment declined.")[:200])
    except Exception:
        log.exception("pre_checkout_query answer failed")


async def on_successful_payment(update, context) -> None:  # noqa: ANN001
    msg = update.effective_message
    payment = getattr(msg, "successful_payment", None) if msg else None
    if payment is None:
        return
    ok, note = apply_successful_payment(
        getattr(payment, "invoice_payload", "") or "",
        getattr(payment, "telegram_payment_charge_id", "") or "",
    )
    if not ok:
        log.info("successful_payment ignored: %s", note)
        return
    try:
        await msg.reply_text(
            "Payment received. The shop will confirm and ship your order."
        )
    except Exception:
        log.warning("successful_payment buyer ack failed")


def register_payment_handlers(app) -> None:  # noqa: ANN001
    """Always register handlers. Send path stays off without a provider token."""
    from telegram.ext import MessageHandler, PreCheckoutQueryHandler, filters

    app.add_handler(PreCheckoutQueryHandler(on_pre_checkout_query))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
