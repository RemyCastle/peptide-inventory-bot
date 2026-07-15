"""
Shop clones (shared inventory, separate prices) + master-only service fees.

- Owner/admin of a shop can clone it into another group.
- Clone products keep their own prices but stock reads/writes the master product.
- OWNER_IDS only: per-shop hidden service fee rolled into shipping (customer never sees a line item).
- Weekly invoices for those fees (master only).
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import (
    _insert_audit,
    _utc_now,
    add_admin,
    ensure_shop,
    get_db,
    get_product,
    get_shop,
    is_admin,
    is_owner,
    list_products,
    shop_display,
)


def ensure_franchise_tables() -> None:
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(shops)").fetchall()}
        if "inventory_master_chat_id" not in cols:
            conn.execute(
                "ALTER TABLE shops ADD COLUMN inventory_master_chat_id INTEGER"
            )
        if "hidden_service_fee" not in cols:
            conn.execute(
                "ALTER TABLE shops ADD COLUMN hidden_service_fee REAL NOT NULL DEFAULT 0"
            )
        if "clone_of_chat_id" not in cols:
            conn.execute("ALTER TABLE shops ADD COLUMN clone_of_chat_id INTEGER")

        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "linked_product_id" not in pcols:
            conn.execute("ALTER TABLE products ADD COLUMN linked_product_id INTEGER")

        ocols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "hidden_service_fee" not in ocols:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN hidden_service_fee REAL NOT NULL DEFAULT 0"
            )
        # Franchisee → main shop remittance proof (do not forward sale until set)
        if "franchise_master_proof_file_id" not in ocols:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN franchise_master_proof_file_id TEXT"
            )
        if "franchise_master_proof_file_type" not in ocols:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN franchise_master_proof_file_type TEXT"
            )
        if "franchise_forwarded_to_master_at" not in ocols:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN franchise_forwarded_to_master_at TEXT"
            )

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shop_clone_tokens (
                token           TEXT PRIMARY KEY,
                source_chat_id  INTEGER NOT NULL,
                created_by      INTEGER NOT NULL,
                target_chat_id  INTEGER,
                status          TEXT NOT NULL DEFAULT 'pending',
                -- pending | used | revoked
                created_at      TEXT NOT NULL,
                used_at         TEXT
            );

            CREATE TABLE IF NOT EXISTS service_fee_invoices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id         INTEGER NOT NULL,
                week_start      TEXT NOT NULL,
                week_end        TEXT NOT NULL,
                order_count     INTEGER NOT NULL DEFAULT 0,
                total_fees      REAL NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'open',
                -- open | paid | waived
                note            TEXT,
                created_at      TEXT NOT NULL,
                paid_at         TEXT,
                UNIQUE(chat_id, week_start, week_end)
            );

            CREATE INDEX IF NOT EXISTS idx_products_linked
                ON products(linked_product_id);
            CREATE INDEX IF NOT EXISTS idx_orders_service_fee
                ON orders(chat_id, paid_at);
            CREATE INDEX IF NOT EXISTS idx_invoices_status
                ON service_fee_invoices(status, chat_id);
            """
        )


def is_franchisee_shop(chat_id: int) -> bool:
    """True if this shop pulls inventory from a master (clone / franchise group)."""
    ensure_franchise_tables()
    shop = get_shop(int(chat_id))
    if not shop:
        return False
    return shop.get("inventory_master_chat_id") is not None


def master_chat_id_for(chat_id: int) -> Optional[int]:
    ensure_franchise_tables()
    shop = get_shop(int(chat_id))
    if not shop:
        return None
    mid = shop.get("inventory_master_chat_id")
    return int(mid) if mid is not None else None


def set_franchise_master_proof(
    order_id: int,
    file_id: str,
    file_type: str,
) -> tuple[bool, str]:
    """Attach franchisee→main remittance proof. Order must already be paid."""
    ensure_franchise_tables()
    fid = (file_id or "").strip()
    ftype = (file_type or "photo").strip().lower()
    if not fid:
        return False, "Missing proof file."
    if ftype not in ("photo", "document"):
        ftype = "photo"
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (int(order_id),)
        ).fetchone()
        if not order:
            return False, "Order not found."
        if order["status"] != "paid":
            return False, "Confirm customer payment first, then send proof to main shop."
        if not is_franchisee_shop(int(order["chat_id"])):
            return False, "This shop is not a franchisee clone."
        now = _utc_now()
        conn.execute(
            """
            UPDATE orders
            SET franchise_master_proof_file_id = ?,
                franchise_master_proof_file_type = ?,
                franchise_forwarded_to_master_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (fid, ftype, now, now, int(order_id)),
        )
    return True, "Proof saved. Main shop will be notified."


def franchise_forwarded(order: dict) -> bool:
    return bool((order.get("franchise_forwarded_to_master_at") or "").strip())


def resolve_inventory_master(chat_id: int) -> int:
    """Root shop that owns physical stock for this shop."""
    ensure_franchise_tables()
    shop = get_shop(chat_id)
    if not shop:
        return chat_id
    master = shop.get("inventory_master_chat_id")
    if master is None:
        return int(chat_id)
    # one level only (clones of clones still point at root)
    mshop = get_shop(int(master))
    if mshop and mshop.get("inventory_master_chat_id") is not None:
        return int(mshop["inventory_master_chat_id"])
    return int(master)


def stock_root_product_id(product: dict) -> int:
    """Product row that holds real stock."""
    linked = product.get("linked_product_id")
    if linked is not None:
        return int(linked)
    return int(product["id"])


def get_effective_stock(product_id: int) -> Optional[int]:
    p = get_product(product_id)
    if not p:
        return None
    root_id = stock_root_product_id(p)
    if root_id != int(p["id"]):
        root = get_product(root_id)
        if not root:
            return 0
        return int(root["stock"])
    return int(p["stock"])


def enrich_product_stock(p: dict) -> dict:
    """Copy product dict with stock replaced by effective inventory."""
    out = dict(p)
    eff = get_effective_stock(int(p["id"]))
    if eff is not None:
        out["stock"] = eff
        out["display_stock"] = eff
    if p.get("linked_product_id"):
        out["shared_inventory"] = True
        out["inventory_product_id"] = int(p["linked_product_id"])
    else:
        out["shared_inventory"] = False
        out["inventory_product_id"] = int(p["id"])
    return out


def list_products_effective(chat_id: int, active_only: bool = False) -> list[dict]:
    return [enrich_product_stock(p) for p in list_products(chat_id, active_only=active_only)]


def create_clone_token(source_chat_id: int, created_by: int) -> dict:
    """Admin of source (or owner) creates a one-time attach token for a new group."""
    ensure_franchise_tables()
    if not is_admin(source_chat_id, created_by):
        raise PermissionError("Admin only")
    # Always clone from inventory root for product links
    root = resolve_inventory_master(source_chat_id)
    token = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    now = _utc_now()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO shop_clone_tokens
              (token, source_chat_id, created_by, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (token, int(source_chat_id), int(created_by), now),
        )
    return {
        "token": token,
        "source_chat_id": int(source_chat_id),
        "inventory_master_chat_id": root,
        "deep_link_arg": f"clone_{token}",
    }


def get_clone_token(token: str) -> Optional[dict]:
    ensure_franchise_tables()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM shop_clone_tokens WHERE token = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def attach_clone(
    token: str,
    target_chat_id: int,
    by_user: int,
    *,
    title: str | None = None,
) -> tuple[bool, str]:
    """
    Attach a pending clone token to target_chat_id (the new group).
    Copies catalog as price rows linked to master inventory products.
    """
    ensure_franchise_tables()
    inv = get_clone_token(token)
    if not inv:
        return False, "Clone link not found."
    if inv["status"] != "pending":
        return False, f"Clone link is {inv['status']}."
    if not is_admin(int(inv["source_chat_id"]), by_user) and not is_owner(by_user):
        # creator or source admin
        if int(inv["created_by"]) != int(by_user):
            return False, "Only the shop admin who created this link can attach it."

    source_id = int(inv["source_chat_id"])
    if int(target_chat_id) == source_id:
        return False, "Cannot clone a shop onto itself."

    root = resolve_inventory_master(source_id)
    src_shop = get_shop(source_id) or ensure_shop(source_id)
    tgt_title = (title or f"{src_shop.get('title') or 'Shop'} (group)").strip()

    ensure_shop(target_chat_id, title=tgt_title)
    # If target already has products and is not empty clone, refuse
    existing = list_products(target_chat_id)
    tgt = get_shop(target_chat_id)
    if existing and not tgt.get("inventory_master_chat_id"):
        return False, "Target group already has its own catalog. Use an empty group."

    now = _utc_now()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE shops SET
              title = ?,
              inventory_master_chat_id = ?,
              clone_of_chat_id = ?,
              shipping_enabled = ?,
              shipping_fee = ?,
              free_shipping_above = ?,
              shipping_label = ?,
              welcome_text = ?,
              brand_name = ?,
              currency = ?,
              currency_symbol = ?,
              low_stock_threshold = ?,
              setup_complete = 1
            WHERE chat_id = ?
            """,
            (
                tgt_title,
                root,
                source_id,
                int(src_shop.get("shipping_enabled", 1) or 0),
                float(src_shop.get("shipping_fee") or 0),
                float(src_shop.get("free_shipping_above") or 0),
                src_shop.get("shipping_label") or "Standard shipping",
                src_shop.get("welcome_text"),
                src_shop.get("brand_name"),
                src_shop.get("currency"),
                src_shop.get("currency_symbol"),
                src_shop.get("low_stock_threshold"),
                int(target_chat_id),
            ),
        )

        # Source catalog products → clone rows with linked stock
        # Prefer products on source shop; if source is already a clone, still copy its price rows
        # and re-link to root stock product ids.
        src_products = conn.execute(
            """
            SELECT * FROM products
            WHERE chat_id = ? AND active = 1
            ORDER BY sort_order, name
            """,
            (source_id,),
        ).fetchall()

        for sp in src_products:
            link_id = sp["linked_product_id"] or sp["id"]
            # ensure link points at root inventory product
            link_row = conn.execute(
                "SELECT id, chat_id, linked_product_id FROM products WHERE id = ?",
                (int(link_id),),
            ).fetchone()
            if link_row and link_row["linked_product_id"]:
                link_id = int(link_row["linked_product_id"])

            # skip if already cloned this link into target
            already = conn.execute(
                """
                SELECT id FROM products
                WHERE chat_id = ? AND linked_product_id = ?
                """,
                (int(target_chat_id), int(link_id)),
            ).fetchone()
            if already:
                continue

            conn.execute(
                """
                INSERT INTO products (
                    chat_id, name, description, price, stock, unit, active, sort_order,
                    created_at, updated_at, linked_product_id,
                    coa_url, coa_file_id, coa_file_type, coa_filename
                ) VALUES (?, ?, ?, ?, 0, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(target_chat_id),
                    sp["name"],
                    sp["description"] or "",
                    float(sp["price"]),
                    sp["unit"] or "vial",
                    int(sp["sort_order"] or 0),
                    now,
                    now,
                    int(link_id),
                    sp["coa_url"] if "coa_url" in sp.keys() else None,
                    sp["coa_file_id"] if "coa_file_id" in sp.keys() else None,
                    sp["coa_file_type"] if "coa_file_type" in sp.keys() else None,
                    sp["coa_filename"] if "coa_filename" in sp.keys() else None,
                ),
            )

        # Copy active payment methods (separate rails per group is fine)
        pays = conn.execute(
            """
            SELECT * FROM payment_methods
            WHERE chat_id = ? AND active = 1
            ORDER BY sort_order, name
            """,
            (source_id,),
        ).fetchall()
        for pm in pays:
            conn.execute(
                """
                INSERT INTO payment_methods (
                    chat_id, name, instructions, active, sort_order, created_at,
                    method_type, cashtag, handle, chain, address, network_note
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(target_chat_id),
                    pm["name"],
                    pm["instructions"] or "",
                    int(pm["sort_order"] or 0),
                    now,
                    pm["method_type"] if "method_type" in pm.keys() else None,
                    pm["cashtag"] if "cashtag" in pm.keys() else None,
                    pm["handle"] if "handle" in pm.keys() else None,
                    pm["chain"] if "chain" in pm.keys() else None,
                    pm["address"] if "address" in pm.keys() else None,
                    pm["network_note"] if "network_note" in pm.keys() else None,
                ),
            )

        conn.execute(
            """
            UPDATE shop_clone_tokens
            SET status = 'used', target_chat_id = ?, used_at = ?
            WHERE token = ?
            """,
            (int(target_chat_id), now, token),
        )

    add_admin(int(target_chat_id), by_user, None, by_user)
    n = len(list_products(target_chat_id))
    return (
        True,
        f"Shop cloned into this group. {n} products share inventory with master `{root}` "
        f"but use this group's prices. Adjust prices under Admin → Products.",
    )


def adjust_stock_effective(
    product_id: int,
    delta: int,
    *,
    actor_id: int | None = None,
    reason: str = "manual_adjust",
    order_id: int | None = None,
) -> Optional[int]:
    """Adjust stock on the root inventory product."""
    ensure_franchise_tables()
    p = get_product(product_id)
    if not p:
        return None
    root_id = stock_root_product_id(p)
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, chat_id, name, stock FROM products WHERE id = ?",
            (root_id,),
        ).fetchone()
        if not row:
            return None
        before = int(row["stock"])
        new_stock = max(0, before + int(delta))
        applied = new_stock - before
        conn.execute(
            "UPDATE products SET stock = ?, updated_at = ? WHERE id = ?",
            (new_stock, _utc_now(), root_id),
        )
        if applied != 0:
            _insert_audit(
                conn,
                chat_id=int(row["chat_id"]),
                product_id=int(root_id),
                product_name=row["name"],
                delta=applied,
                stock_before=before,
                stock_after=new_stock,
                reason=reason,
                actor_id=actor_id,
                order_id=order_id,
            )
        return new_stock


# ── Master-only service fee ──────────────────────────────────────────────────


def set_hidden_service_fee(chat_id: int, fee: float, by_user: int) -> tuple[bool, str]:
    if not is_owner(by_user):
        return False, "Master admin only."
    if fee < 0:
        return False, "Fee cannot be negative."
    ensure_franchise_tables()
    ensure_shop(chat_id)
    with get_db() as conn:
        conn.execute(
            "UPDATE shops SET hidden_service_fee = ? WHERE chat_id = ?",
            (float(fee), int(chat_id)),
        )
    return True, f"Hidden service fee for shop `{chat_id}` set to {fee:.2f}."


def get_hidden_service_fee(chat_id: int) -> float:
    ensure_franchise_tables()
    shop = get_shop(chat_id)
    if not shop:
        return 0.0
    return float(shop.get("hidden_service_fee") or 0)


def customer_shipping_total(shop: dict, subtotal: float) -> tuple[float, float]:
    """
    Returns (amount_customer_pays_as_shipping, hidden_service_fee_portion).
    Hidden fee is folded into shipping so the customer only sees one shipping number.
    """
    from db import calc_shipping

    base = float(calc_shipping(shop, subtotal))
    hidden = float(shop.get("hidden_service_fee") or 0)
    if hidden < 0:
        hidden = 0.0
    # Always add hidden fee on orders when shipping path is used OR always on every order
    # Spec: "added to every orders shipping" — add even if base shipping is 0 / free threshold
    return round(base + hidden, 2), round(hidden, 2)


def list_shops_service_fees() -> list[dict]:
    ensure_franchise_tables()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, title, hidden_service_fee, inventory_master_chat_id, clone_of_chat_id
            FROM shops
            ORDER BY title
            """
        ).fetchall()
        return [dict(r) for r in rows]


def _week_bounds(ref: datetime | None = None) -> tuple[str, str, datetime, datetime]:
    """UTC week Mon 00:00 → next Mon 00:00 (ISO-like). Returns (start_str, end_str, start_dt, end_dt)."""
    now = ref or datetime.now(timezone.utc)
    # Monday = 0
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    end = start + timedelta(days=7)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt), start, end


def generate_weekly_invoices(
    by_user: int, *, ref: datetime | None = None
) -> tuple[bool, str, list[dict]]:
    """Roll up paid-order hidden fees for the current UTC week per shop. Master only."""
    if not is_owner(by_user):
        return False, "Master admin only.", []
    ensure_franchise_tables()
    week_start, week_end, _, _ = _week_bounds(ref)
    created: list[dict] = []
    with get_db() as conn:
        shops = conn.execute("SELECT chat_id, title FROM shops").fetchall()
        for s in shops:
            cid = int(s["chat_id"])
            row = conn.execute(
                """
                SELECT COUNT(*) AS c, COALESCE(SUM(hidden_service_fee), 0) AS total
                FROM orders
                WHERE chat_id = ?
                  AND status = 'paid'
                  AND paid_at IS NOT NULL
                  AND paid_at >= ? AND paid_at < ?
                  AND hidden_service_fee > 0
                """,
                (cid, week_start, week_end),
            ).fetchone()
            count = int(row["c"] or 0)
            total = float(row["total"] or 0)
            if total <= 0 and count <= 0:
                continue
            now = _utc_now()
            existing = conn.execute(
                """
                SELECT * FROM service_fee_invoices
                WHERE chat_id = ? AND week_start = ? AND week_end = ?
                """,
                (cid, week_start, week_end),
            ).fetchone()
            if existing:
                if existing["status"] == "paid":
                    continue
                conn.execute(
                    """
                    UPDATE service_fee_invoices
                    SET order_count = ?, total_fees = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (count, total, now, int(existing["id"])),
                )
                inv = conn.execute(
                    "SELECT * FROM service_fee_invoices WHERE id = ?",
                    (int(existing["id"]),),
                ).fetchone()
            else:
                cur = conn.execute(
                    """
                    INSERT INTO service_fee_invoices
                      (chat_id, week_start, week_end, order_count, total_fees, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (cid, week_start, week_end, count, total, now),
                )
                inv = conn.execute(
                    "SELECT * FROM service_fee_invoices WHERE id = ?",
                    (int(cur.lastrowid),),
                ).fetchone()
            d = dict(inv)
            d["title"] = s["title"]
            created.append(d)
    return True, f"Weekly invoices ready ({week_start} → {week_end} UTC).", created


def list_invoices(
    *,
    status: str | None = "open",
    chat_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    ensure_franchise_tables()
    with get_db() as conn:
        q = """
            SELECT i.*, s.title
            FROM service_fee_invoices i
            LEFT JOIN shops s ON s.chat_id = i.chat_id
            WHERE 1=1
        """
        args: list[Any] = []
        if status:
            q += " AND i.status = ?"
            args.append(status)
        if chat_id is not None:
            q += " AND i.chat_id = ?"
            args.append(int(chat_id))
        q += " ORDER BY i.week_start DESC, i.id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def mark_invoice_paid(invoice_id: int, by_user: int, note: str = "") -> tuple[bool, str]:
    if not is_owner(by_user):
        return False, "Master admin only."
    ensure_franchise_tables()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM service_fee_invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        if not row:
            return False, "Invoice not found."
        if row["status"] == "paid":
            return False, "Already paid."
        conn.execute(
            """
            UPDATE service_fee_invoices
            SET status = 'paid', paid_at = ?, note = ?
            WHERE id = ?
            """,
            (_utc_now(), note or f"Marked paid by {by_user}", invoice_id),
        )
    return True, f"Invoice #{invoice_id} marked paid."


def service_fee_ledger(chat_id: int | None = None, limit: int = 40) -> list[dict]:
    """Paid orders that carried a hidden service fee."""
    ensure_franchise_tables()
    with get_db() as conn:
        if chat_id is not None:
            rows = conn.execute(
                """
                SELECT id, chat_id, payment_code, total, shipping_fee, hidden_service_fee,
                       status, paid_at, created_at
                FROM orders
                WHERE chat_id = ? AND hidden_service_fee > 0
                ORDER BY id DESC LIMIT ?
                """,
                (int(chat_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, chat_id, payment_code, total, shipping_fee, hidden_service_fee,
                       status, paid_at, created_at
                FROM orders
                WHERE hidden_service_fee > 0
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
