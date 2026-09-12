"""Read-only plain-text report generators for admin export."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import db


def _shop_title(shop: dict | None, shop_id: int) -> str:
    raw = (shop or {}).get("title")
    if raw:
        try:
            from catalog_cleanup import storefront_label

            return storefront_label(raw, 80) or f"Shop {shop_id}"
        except Exception:
            return str(raw)
    return f"Shop {shop_id}"


def _sym(shop: dict | None) -> str:
    return db.shop_display(shop)["currency_symbol"]


def _report_label(raw: object, max_len: int = 80) -> str:
    try:
        from catalog_cleanup import storefront_label

        return storefront_label(raw, max_len)
    except Exception:
        return " ".join(str(raw or "").split())[:max_len]


def _report_product(raw: object) -> str:
    try:
        from catalog_cleanup import display_product_name

        return display_product_name(str(raw or ""))
    except Exception:
        return str(raw or "")


def generate_inventory_report(shop_id: int) -> str:
    """All products for a shop: name, price, stock, low-stock flag."""
    shop = db.get_shop(shop_id)
    display = db.shop_display(shop)
    threshold = int(display["low_stock_threshold"])
    sym = display["currency_symbol"]
    products = db.list_products(shop_id, active_only=False)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"INVENTORY REPORT — {_shop_title(shop, shop_id)}",
        f"Shop ID: {shop_id}",
        f"Generated: {now}",
        f"Low-stock threshold: <= {threshold}",
        "-" * 56,
    ]
    if not products:
        lines.append("No products in catalog.")
        lines.append("-" * 56)
        return "\n".join(lines) + "\n"

    lines.append(f"{'ID':>5}  {'Name':<28}  {'Price':>10}  {'Stock':>6}  Flag")
    lines.append("-" * 56)
    for p in products:
        stock = int(p.get("stock") or 0)
        active = "on" if p.get("active") else "off"
        flag = ""
        if stock <= threshold:
            flag = "LOW" if stock > 0 else "OUT"
        if not p.get("active"):
            flag = (flag + " INACTIVE").strip()
        try:
            from catalog_cleanup import display_product_name

            name = display_product_name(str(p.get("name") or ""))[:28]
        except Exception:
            name = str(p.get("name") or "")[:28]
        price = float(p.get("price") or 0)
        lines.append(
            f"{int(p['id']):>5}  {name:<28}  {sym}{price:>8.2f}  {stock:>6}  {flag} [{active}]"
        )
    lines.append("-" * 56)
    lines.append(f"Total products: {len(products)}")
    return "\n".join(lines) + "\n"


def generate_pending_orders_report(shop_id: int) -> str:
    """Orders in pending_payment or awaiting_confirmation only."""
    shop = db.get_shop(shop_id)
    sym = _sym(shop)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pending = db.list_orders(shop_id, status="pending_payment", limit=500)
    awaiting = db.list_orders(shop_id, status="awaiting_confirmation", limit=500)
    # newest first overall
    orders = sorted(
        pending + awaiting,
        key=lambda o: int(o["id"]),
        reverse=True,
    )

    lines = [
        f"PENDING ORDERS REPORT — {_shop_title(shop, shop_id)}",
        f"Shop ID: {shop_id}",
        f"Generated: {now}",
        "Statuses: pending_payment, awaiting_confirmation",
        "-" * 56,
    ]
    if not orders:
        lines.append("No pending orders.")
        lines.append("-" * 56)
        return "\n".join(lines) + "\n"

    for o in orders:
        items = db.get_order_items(int(o["id"]))
        buyer = (
            _report_label(o.get("full_name"), 80)
            or _report_label(o.get("username"), 80)
            or o.get("user_id")
        )
        method = _report_label(o.get("payment_method_name"), 60) or "—"
        lines.append(
            f"Order #{o['id']} | {o['status']} | {o.get('created_at')}"
        )
        lines.append(f"  Buyer: {buyer} (id {o.get('user_id')})")
        lines.append(f"  Method: {method}")
        lines.append(
            f"  Total: {sym}{float(o.get('total') or 0):.2f} "
            f"(sub {sym}{float(o.get('subtotal') or 0):.2f} + ship {sym}{float(o.get('shipping_fee') or 0):.2f})"
        )
        if items:
            lines.append("  Items:")
            for it in items:
                shown = _report_product(it.get("product_name"))
                lines.append(
                    f"    - {shown} x{it.get('quantity')} "
                    f"= {sym}{float(it.get('line_total') or 0):.2f}"
                )
        else:
            lines.append("  Items: (none)")
        lines.append("")

    lines.append("-" * 56)
    lines.append(f"Total pending orders: {len(orders)}")
    return "\n".join(lines) + "\n"


def generate_full_report(shop_id: int) -> str:
    """Inventory + pending orders in one document."""
    inv = generate_inventory_report(shop_id).rstrip()
    pend = generate_pending_orders_report(shop_id).rstrip()
    return inv + "\n\n" + "=" * 56 + "\n\n" + pend + "\n"


def safe_filename_part(text: str, max_len: int = 32) -> str:
    """Sanitize shop title for filenames."""
    try:
        from catalog_cleanup import storefront_label

        text = storefront_label(text, max_len) or "shop"
    except Exception:
        text = text or "shop"
    out = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    s = "".join(out).strip("_") or "shop"
    return s[:max_len]
