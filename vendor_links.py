"""Explicit SPBC-catalog → vendor-product mapping.

Routing normally matches a vendor's product by name, which quietly fails when
a vendor names things their own way: Unicorn's "H36" is SPBC's "HGH 360IU",
and no amount of normalising connects those. A vendor that can actually fill
an order then never gets quoted, and it looks like a bug rather than a naming
mismatch.

This lets the SPBC admin say, once, "our HGH 360IU is her H36". Mappings are
per (spbc product name → vendor shop), and routing prefers them over the name
match while leaving unmapped products exactly as they were.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import db

log = logging.getLogger("vendor_links")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables() -> None:
    with db.get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_product_links (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                spbc_name         TEXT NOT NULL,
                shop_chat_id      INTEGER NOT NULL,
                vendor_product_id INTEGER NOT NULL,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                UNIQUE (spbc_name, shop_chat_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vlinks_shop "
            "ON vendor_product_links(shop_chat_id)"
        )


def _key(name: str) -> str:
    return " ".join(str(name or "").split()).lower()


def set_link(spbc_name: str, shop_chat_id: int, vendor_product_id: int) -> tuple[bool, str]:
    """Point an SPBC catalog product at one vendor's product."""
    name = " ".join(str(spbc_name or "").split())
    if not name:
        return False, "SPBC product name required."
    product = db.get_product(int(vendor_product_id))
    if not product:
        return False, "Vendor product not found."
    if int(product["chat_id"]) != int(shop_chat_id):
        return False, "That product belongs to a different shop."
    ensure_tables()
    now = _utc_now()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO vendor_product_links
                (spbc_name, shop_chat_id, vendor_product_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(spbc_name, shop_chat_id) DO UPDATE SET
                vendor_product_id = excluded.vendor_product_id,
                updated_at = excluded.updated_at
            """,
            (_key(name), int(shop_chat_id), int(vendor_product_id), now, now),
        )
    return True, f"{name} → {product['name']}"


def clear_link(spbc_name: str, shop_chat_id: int) -> bool:
    ensure_tables()
    with db.get_db() as conn:
        cur = conn.execute(
            "DELETE FROM vendor_product_links WHERE spbc_name = ? AND shop_chat_id = ?",
            (_key(spbc_name), int(shop_chat_id)),
        )
        return cur.rowcount > 0


def product_for(spbc_name: str, shop_chat_id: int) -> Optional[dict]:
    """The vendor's product mapped to this SPBC name, if any (and still live)."""
    ensure_tables()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT vendor_product_id FROM vendor_product_links "
            "WHERE spbc_name = ? AND shop_chat_id = ?",
            (_key(spbc_name), int(shop_chat_id)),
        ).fetchone()
    if not row:
        return None
    product = db.get_product(int(row["vendor_product_id"]))
    if not product or int(product["chat_id"]) != int(shop_chat_id):
        return None
    if not int(product.get("active") or 0):
        return None
    return product


def links_for_shop(shop_chat_id: int) -> list[dict]:
    ensure_tables()
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM vendor_product_links WHERE shop_chat_id = ? ORDER BY spbc_name",
            (int(shop_chat_id),),
        ).fetchall()
    out = []
    for r in rows:
        p = db.get_product(int(r["vendor_product_id"]))
        out.append(
            {
                "spbc_name": r["spbc_name"],
                "shop_chat_id": int(r["shop_chat_id"]),
                "vendor_product_id": int(r["vendor_product_id"]),
                "vendor_product_name": (p or {}).get("name"),
                "vendor_price": (p or {}).get("price"),
                "vendor_stock": (p or {}).get("stock"),
                "missing": p is None,
            }
        )
    return out


def vendor_catalogs(exclude_shop_id: int | None = None) -> list[dict]:
    """Every vendor shop with its products — the picker's data source."""
    ensure_tables()
    with db.get_db() as conn:
        shops = [dict(r) for r in conn.execute(
            "SELECT chat_id, title FROM shops ORDER BY title"
        ).fetchall()]
    out = []
    for s in shops:
        sid = int(s["chat_id"])
        if exclude_shop_id and sid == int(exclude_shop_id):
            continue
        products = db.list_products(sid, active_only=True)
        if not products:
            continue
        out.append(
            {
                "shop_chat_id": sid,
                "title": s["title"],
                "products": [
                    {
                        "id": int(p["id"]),
                        "name": p["name"],
                        "price": float(p["price"] or 0),
                        "kit_price": p.get("kit_price"),
                        "stock": int(p.get("stock") or 0),
                    }
                    for p in products
                ],
                "links": links_for_shop(sid),
            }
        )
    return out
