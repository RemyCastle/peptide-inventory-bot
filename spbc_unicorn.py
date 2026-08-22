"""Paid Springfield PBC website orders → Unicorn Magic Factory shop.

POST /notify already alerts Telegram. This module adds the missing shop
row: when status is paid (or shipped/complete), create one Unicorn order
already marked paid. The customer paid on SPBC — no Venmo/PayPal/Telegram
invoice.

Idempotent on ``spbc:{order_number}``. Unmatched SKUs stay on the order
as notes; no fake catalog products are created.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import db
from config import (
    OWNER_IDS,
    UNICORN_SHOP_CHAT_ID,
)
from order_router import parse_line
from spbc_notify import format_ship_lines, money_from_cents, strip_kind_suffix

log = logging.getLogger("spbc_unicorn")

PAID_IMPORT_STATUSES = {"paid", "shipped", "complete"}
EXTERNAL_REF_PREFIX = "spbc:"
PAYMENT_LABEL = "SPBC website (already paid)"
SYSTEM_USER_ID = 0


def external_ref_for(order_number: Any) -> str:
    raw = str(order_number or "").strip()
    return f"{EXTERNAL_REF_PREFIX}{raw}" if raw else ""


def resolve_unicorn_shop_chat_id() -> int:
    """Unicorn fulfillment shop. Never invents a Telegram chat id.

    Order: UNICORN_SHOP_CHAT_ID → vendor Unicorn config → 0 (skip import).
    SPBC_SHOP_CHAT_ID is not used as a silent fallback (different shop).
    """
    if UNICORN_SHOP_CHAT_ID:
        try:
            return int(db.resolve_shop_chat_id(int(UNICORN_SHOP_CHAT_ID)))
        except Exception:
            return int(UNICORN_SHOP_CHAT_ID)
    try:
        import vendor_stores

        for v in vendor_stores.load_vendor_configs():
            name = (v.get("name") or "").lower()
            if "unicorn" not in name:
                continue
            sid = vendor_stores._resolve_shop(v)
            if sid:
                return int(sid)
    except Exception as exc:
        log.warning("unicorn shop resolve via vendor config failed: %s", exc)
    return 0


def _norm(name: str) -> str:
    return " ".join(str(name or "").split()).lower()


def _item_sku(item: dict) -> str:
    return str(item.get("sku") or item.get("SKU") or "").strip()


def _item_label(item: dict) -> str:
    return str(item.get("name") or item.get("sku") or "Item").strip() or "Item"


def match_unicorn_product(shop_chat_id: int, item: dict) -> Optional[dict]:
    """Exact-ish match on sku or name. No fuzzy invent, no new products."""
    if not isinstance(item, dict):
        return None
    products = db.list_products(int(shop_chat_id), active_only=False)
    if not products:
        return None

    sku = _item_sku(item)
    parsed = parse_line(item)
    base = parsed["base"] if parsed else strip_kind_suffix(_item_label(item))
    label = _item_label(item)

    by_sku: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for p in products:
        psku = str(p.get("sku") or "").strip()
        if psku:
            by_sku.setdefault(_norm(psku), p)
        by_name.setdefault(_norm(p.get("name") or ""), p)
        stripped = _norm(strip_kind_suffix(p.get("name") or ""))
        if stripped:
            by_name.setdefault(stripped, p)

    if sku and _norm(sku) in by_sku:
        return by_sku[_norm(sku)]
    if sku and _norm(sku) in by_name:
        return by_name[_norm(sku)]

    for key in (_norm(label), _norm(base), _norm(strip_kind_suffix(label))):
        if key and key in by_name:
            return by_name[key]

    try:
        import vendor_links

        linked = vendor_links.product_for(base or label, int(shop_chat_id))
        if linked:
            return linked
    except Exception as exc:
        log.warning("vendor_links match failed: %s", exc)
    return None


def map_notify_items(shop_chat_id: int, payload: dict) -> tuple[list[dict], list[str]]:
    """Build order_items rows + unmatched labels. Never invents products."""
    raw = payload.get("items")
    raw = raw if isinstance(raw, list) else []
    mapped: list[dict] = []
    unmatched: list[str] = []

    for it in raw:
        if not isinstance(it, dict):
            continue
        parsed = parse_line(it)
        label = _item_label(it)
        sku = _item_sku(it)
        if parsed:
            qty = int(parsed["qty"])
            kind = parsed["kind"]
        else:
            try:
                qty = int(it.get("qty") or it.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0
            kind = "vial"
        if qty <= 0:
            qty = 1

        prod = match_unicorn_product(shop_chat_id, it)
        if prod is None:
            hint = f"{qty}× {label}"
            if sku:
                hint += f" (sku {sku})"
            unmatched.append(hint)
            mapped.append(
                {
                    "product_id": None,
                    "product_name": f"{label} [unmatched SPBC item]",
                    "unit_price": 0.0,
                    "quantity": qty,
                }
            )
            continue

        unit_price = float(prod.get("price") or 0)
        name = str(prod.get("name") or label)
        quantity = qty
        if kind == "kit":
            from config import KIT_SIZE

            kp = prod.get("kit_price")
            try:
                kit_price = float(kp) if kp not in (None, "", 0, 0.0) else None
            except (TypeError, ValueError):
                kit_price = None
            if kit_price is not None and kit_price > 0:
                name = f"{name} (kit of {KIT_SIZE})"
                unit_price = kit_price / float(KIT_SIZE)
                quantity = qty * int(KIT_SIZE)
            else:
                name = f"{name} (kit)"
                quantity = qty * int(KIT_SIZE)

        mapped.append(
            {
                "product_id": int(prod["id"]),
                "product_name": name,
                "unit_price": unit_price,
                "quantity": quantity,
            }
        )
    return mapped, unmatched


def _ship_fields(payload: dict) -> tuple[str, str]:
    ship = payload.get("shipping")
    if not isinstance(ship, dict):
        return (
            str(payload.get("customer_name") or payload.get("customerName") or "").strip(),
            "",
        )
    name = str(ship.get("name") or payload.get("customer_name") or "").strip()
    addr = "\n".join(format_ship_lines(ship))
    return name, addr


def _build_notes(
    payload: dict, unmatched: list[str], stock_note: str = ""
) -> tuple[str, str]:
    order_number = payload.get("order_number") or payload.get("orderNumber") or ""
    site_total = money_from_cents(
        payload.get("total_cents", payload.get("totalCents"))
    ) or (str(payload["total"]) if payload.get("total") is not None else "")
    ship_bits = [
        f"Paid on Springfield PBC (SPBC {order_number}). Do not invoice.",
    ]
    if site_total:
        ship_bits.append(f"SPBC total: {site_total}")
    note = str(payload.get("note") or "").strip()
    if note:
        ship_bits.append(note[:500])
    if unmatched:
        ship_bits.append("Unmatched SPBC items (not in Unicorn catalog):")
        ship_bits.extend(f"• {u}" for u in unmatched)
    if stock_note:
        ship_bits.append(stock_note)
    ship_notes = "\n".join(ship_bits)
    admin_note = (
        f"Imported from paid SPBC order {order_number}. Already paid on the website."
    )
    if unmatched:
        admin_note += " Unmatched items: " + "; ".join(unmatched)
    return ship_notes, admin_note


def _confirm_actor_id() -> int:
    return min(OWNER_IDS) if OWNER_IDS else SYSTEM_USER_ID


def _try_confirm_paid(order: dict, unmatched: list[str], payload: dict) -> dict:
    """Same deduct path as admin confirm. Order stays paid even if stock is short."""
    oid = int(order["id"])
    if str(order.get("status") or "") == "paid":
        return order
    ok, msg, _alerts = db.confirm_order_payment(oid, _confirm_actor_id())
    if ok:
        return db.get_order(oid) or order
    log.warning(
        "spbc unicorn confirm stock/path failed order=%s: %s", oid, msg
    )
    now = db._utc_now()
    stock_note = f"Stock not deducted automatically: {msg}"
    ship_notes, admin_note = _build_notes(payload, unmatched, stock_note)
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE orders
            SET status = 'paid', paid_at = COALESCE(paid_at, ?),
                updated_at = ?, ship_notes = ?, admin_note = ?,
                payment_method_name = COALESCE(payment_method_name, ?)
            WHERE id = ?
            """,
            (now, now, ship_notes, admin_note, PAYMENT_LABEL, oid),
        )
    return db.get_order(oid) or order


def import_paid_spbc_order(payload: dict) -> dict:
    """Create or reuse one Unicorn paid order for a paid SPBC notify payload.

    Returns a JSON-safe dict for the /notify response. Never raises to the
    HTTP layer — failures are ``ok: False`` so supplier alerts still send.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad_payload"}
    status = str(payload.get("status") or "").lower()
    if status not in PAID_IMPORT_STATUSES:
        return {"ok": False, "error": "not_paid", "status": payload.get("status")}

    order_number = payload.get("order_number") or payload.get("orderNumber")
    order_number = str(order_number or "").strip()
    if not order_number:
        return {"ok": False, "error": "order_number required"}

    shop_id = resolve_unicorn_shop_chat_id()
    if not shop_id:
        log.warning(
            "paid SPBC %s: Unicorn shop not configured "
            "(set UNICORN_SHOP_CHAT_ID; do not invent a chat id)",
            order_number,
        )
        return {
            "ok": False,
            "error": "unicorn_shop_not_configured",
            "message": "Set UNICORN_SHOP_CHAT_ID to the Unicorn shop chat id "
            "(or bind the Unicorn vendor invite). Import skipped.",
        }

    shop = db.get_shop(int(shop_id))
    if not shop:
        return {
            "ok": False,
            "error": "unicorn_shop_missing",
            "shop_chat_id": int(shop_id),
            "message": "UNICORN_SHOP_CHAT_ID is set but that shop is not in the DB.",
        }

    ref = external_ref_for(order_number)
    existing = db.get_order_by_external_ref(ref)
    if existing:
        if int(existing.get("chat_id") or 0) != int(shop_id):
            return {
                "ok": True,
                "duplicate": True,
                "order_id": int(existing["id"]),
                "shop_chat_id": int(existing["chat_id"]),
                "status": existing.get("status"),
                "external_ref": ref,
            }
        if str(existing.get("status") or "") != "paid":
            existing = _try_confirm_paid(existing, [], payload)
        return {
            "ok": True,
            "duplicate": True,
            "order_id": int(existing["id"]),
            "shop_chat_id": int(shop_id),
            "status": existing.get("status"),
            "external_ref": ref,
            "unmatched": [],
        }

    mapped, unmatched = map_notify_items(shop_id, payload)
    if not mapped:
        mapped = []
        unmatched = unmatched or ["(no line items in payload)"]
        mapped.append(
            {
                "product_id": None,
                "product_name": "[unmatched SPBC item] (no line items)",
                "unit_price": 0.0,
                "quantity": 0,
            }
        )

    ship_name, ship_address = _ship_fields(payload)
    ship_notes, admin_note = _build_notes(payload, unmatched)
    customer = (
        payload.get("customer_name")
        or payload.get("customerName")
        or ship_name
        or "SPBC customer"
    )

    order = db.create_imported_order(
        shop_id,
        user_id=SYSTEM_USER_ID,
        username="spbc-website",
        full_name=str(customer)[:120],
        items=mapped,
        payment_method_name=PAYMENT_LABEL,
        ship_name=ship_name or str(customer),
        ship_address=ship_address,
        ship_notes=ship_notes,
        admin_note=admin_note,
        external_ref=ref,
        shipping_fee=0.0,
    )
    if not order:
        return {"ok": False, "error": "create_failed", "external_ref": ref}

    # Unique-index race: create_imported_order returns the existing row.
    if str(order.get("status") or "") == "paid":
        return {
            "ok": True,
            "duplicate": True,
            "order_id": int(order["id"]),
            "shop_chat_id": int(shop_id),
            "status": "paid",
            "external_ref": ref,
            "unmatched": unmatched,
        }

    order = _try_confirm_paid(order, unmatched, payload)
    return {
        "ok": True,
        "duplicate": False,
        "order_id": int(order["id"]),
        "shop_chat_id": int(shop_id),
        "status": order.get("status"),
        "external_ref": ref,
        "unmatched": unmatched,
        "payment_requested": False,
    }


def maybe_import_paid_spbc_order(payload: dict) -> Optional[dict]:
    """Call from /notify. None when status is not a paid import."""
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "").lower()
    if status not in PAID_IMPORT_STATUSES:
        return None
    try:
        return import_paid_spbc_order(payload)
    except Exception as exc:
        log.exception("paid SPBC → Unicorn import failed: %s", exc)
        return {"ok": False, "error": "import_failed", "message": str(exc)}
