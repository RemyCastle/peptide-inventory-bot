"""
Shop-to-shop inventory collaboration.

Host shop invites guest shop via special link.
Host selects guest products + markup %.
Customer pays host; stock deducts per owner shop;
host settles guest payout.
"""

from __future__ import annotations

import secrets
import string
from typing import Any, Optional

from db import (
    _insert_audit,
    _utc_now,
    ensure_shop,
    get_db,
    get_product,
    get_shop,
    is_admin,
    shop_display,
)


def _token(n: int = 12) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def ensure_collab_tables() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shop_invites (
                token           TEXT PRIMARY KEY,
                host_chat_id    INTEGER NOT NULL,
                created_by      INTEGER NOT NULL,
                guest_chat_id   INTEGER,
                status          TEXT NOT NULL DEFAULT 'pending',
                -- pending | accepted | revoked
                default_markup_pct REAL NOT NULL DEFAULT 15,
                created_at      TEXT NOT NULL,
                accepted_at     TEXT,
                FOREIGN KEY (host_chat_id) REFERENCES shops(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shop_shares (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                host_chat_id    INTEGER NOT NULL,
                guest_chat_id   INTEGER NOT NULL,
                product_id      INTEGER NOT NULL,
                markup_pct      REAL NOT NULL DEFAULT 15,
                active          INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                UNIQUE(host_chat_id, product_id),
                FOREIGN KEY (host_chat_id) REFERENCES shops(chat_id) ON DELETE CASCADE,
                FOREIGN KEY (guest_chat_id) REFERENCES shops(chat_id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS order_settlements (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        INTEGER NOT NULL,
                host_chat_id    INTEGER NOT NULL,
                guest_chat_id   INTEGER NOT NULL,
                amount          REAL NOT NULL,
                status          TEXT NOT NULL DEFAULT 'owed',
                -- owed | paid | waived
                paid_at         TEXT,
                note            TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_shares_host ON shop_shares(host_chat_id, active);
            CREATE INDEX IF NOT EXISTS idx_settlements_guest ON order_settlements(guest_chat_id, status);
            """
        )
        # Multi-owner line columns
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(order_items)").fetchall()}
        if "owner_chat_id" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN owner_chat_id INTEGER")
        if "base_unit_price" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN base_unit_price REAL")
        if "markup_pct" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN markup_pct REAL DEFAULT 0")
        if "is_guest" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN is_guest INTEGER DEFAULT 0")


def create_invite(host_chat_id: int, created_by: int, default_markup_pct: float = 15.0) -> dict:
    ensure_shop(host_chat_id)
    token = _token()
    now = _utc_now()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO shop_invites
              (token, host_chat_id, created_by, status, default_markup_pct, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (token, host_chat_id, created_by, float(default_markup_pct), now),
        )
    return {
        "token": token,
        "host_chat_id": host_chat_id,
        "default_markup_pct": float(default_markup_pct),
        "deep_link_arg": f"collab_{token}",
    }


def get_invite(token: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM shop_invites WHERE token = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def accept_invite(token: str, guest_chat_id: int, user_id: int) -> tuple[bool, str]:
    """Guest shop admin accepts. guest_chat_id must be a shop they admin."""
    if not is_admin(guest_chat_id, user_id):
        return False, "You must be admin of the guest shop to accept."
    inv = get_invite(token)
    if not inv:
        return False, "Invite not found."
    if inv["status"] != "pending":
        return False, f"Invite is {inv['status']}."
    if int(inv["host_chat_id"]) == int(guest_chat_id):
        return False, "Cannot collaborate with your own shop."
    ensure_shop(guest_chat_id)
    with get_db() as conn:
        conn.execute(
            """
            UPDATE shop_invites
            SET status = 'accepted', guest_chat_id = ?, accepted_at = ?
            WHERE token = ?
            """,
            (guest_chat_id, _utc_now(), token),
        )
    return True, "Collaboration accepted. Host can now share your products with markup."


def list_collaborations(host_chat_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT i.*, s.title AS guest_title
            FROM shop_invites i
            LEFT JOIN shops s ON s.chat_id = i.guest_chat_id
            WHERE i.host_chat_id = ? AND i.status = 'accepted'
            ORDER BY i.accepted_at DESC
            """,
            (host_chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_pending_invites(host_chat_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM shop_invites
            WHERE host_chat_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            """,
            (host_chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_share(
    host_chat_id: int,
    guest_chat_id: int,
    product_id: int,
    markup_pct: float,
    active: bool = True,
) -> tuple[bool, str]:
    prod = get_product(product_id)
    if not prod or int(prod["chat_id"]) != int(guest_chat_id):
        return False, "Product does not belong to guest shop."
    if not get_shop(guest_chat_id):
        return False, "Guest shop missing."
    now = _utc_now()
    with get_db() as conn:
        # Ensure accepted collab exists
        ok = conn.execute(
            """
            SELECT 1 FROM shop_invites
            WHERE host_chat_id = ? AND guest_chat_id = ? AND status = 'accepted'
            """,
            (host_chat_id, guest_chat_id),
        ).fetchone()
        if not ok:
            return False, "No accepted collaboration with that guest shop."
        conn.execute(
            """
            INSERT INTO shop_shares
              (host_chat_id, guest_chat_id, product_id, markup_pct, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(host_chat_id, product_id) DO UPDATE SET
              markup_pct = excluded.markup_pct,
              active = excluded.active,
              guest_chat_id = excluded.guest_chat_id,
              updated_at = excluded.updated_at
            """,
            (
                host_chat_id,
                guest_chat_id,
                product_id,
                float(markup_pct),
                1 if active else 0,
                now,
                now,
            ),
        )
    return True, "Share updated."


def list_shares(host_chat_id: int, active_only: bool = True) -> list[dict]:
    with get_db() as conn:
        if active_only:
            rows = conn.execute(
                """
                SELECT sh.*, p.name, p.price AS base_price, p.stock, p.unit, p.active AS product_active,
                       gs.title AS guest_title
                FROM shop_shares sh
                JOIN products p ON p.id = sh.product_id
                LEFT JOIN shops gs ON gs.chat_id = sh.guest_chat_id
                WHERE sh.host_chat_id = ? AND sh.active = 1
                ORDER BY p.name
                """,
                (host_chat_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT sh.*, p.name, p.price AS base_price, p.stock, p.unit, p.active AS product_active,
                       gs.title AS guest_title
                FROM shop_shares sh
                JOIN products p ON p.id = sh.product_id
                LEFT JOIN shops gs ON gs.chat_id = sh.guest_chat_id
                WHERE sh.host_chat_id = ?
                ORDER BY sh.active DESC, p.name
                """,
                (host_chat_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            base = float(d["base_price"])
            mk = float(d["markup_pct"] or 0)
            d["sell_price"] = round(base * (1 + mk / 100.0), 2)
            out.append(d)
        return out


def list_guest_products_for_host(host_chat_id: int, guest_chat_id: int) -> list[dict]:
    """All active products on guest shop with current share settings if any."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   sh.markup_pct AS share_markup,
                   sh.active AS share_active,
                   sh.id AS share_id
            FROM products p
            LEFT JOIN shop_shares sh
              ON sh.product_id = p.id AND sh.host_chat_id = ?
            WHERE p.chat_id = ? AND p.active = 1
            ORDER BY p.name
            """,
            (host_chat_id, guest_chat_id),
        ).fetchall()
        return [dict(r) for r in rows]


def catalog_for_host(host_chat_id: int) -> list[dict]:
    """
    Combined catalog: host own products + shared guest products.
    Each item has owner_chat_id, sell_price, is_guest.
    """
    from db import list_products

    own = list_products(host_chat_id, active_only=True)
    items = []
    for p in own:
        items.append(
            {
                **p,
                "owner_chat_id": host_chat_id,
                "base_price": float(p["price"]),
                "sell_price": float(p["price"]),
                "markup_pct": 0.0,
                "is_guest": False,
                "guest_title": None,
            }
        )
    for sh in list_shares(host_chat_id, active_only=True):
        if not sh.get("product_active"):
            continue
        items.append(
            {
                "id": sh["product_id"],
                "chat_id": sh["guest_chat_id"],
                "name": sh["name"],
                "price": sh["sell_price"],
                "stock": sh["stock"],
                "unit": sh["unit"],
                "owner_chat_id": sh["guest_chat_id"],
                "base_price": float(sh["base_price"]),
                "sell_price": float(sh["sell_price"]),
                "markup_pct": float(sh["markup_pct"]),
                "is_guest": True,
                "guest_title": sh.get("guest_title"),
            }
        )
    return items


def create_order_multi(
    host_chat_id: int,
    user_id: int,
    username: str | None,
    full_name: str | None,
    items: list[dict],
    # items: [{product_id, quantity, owner_chat_id?, unit_price?}]
    payment_method: dict | None,
    ship_name: str,
    ship_address: str,
    ship_notes: str = "",
) -> Optional[dict]:
    """
    Create host order with multi-owner lines.
    Customer pays host; guest lines use sell price; base price stored for settlement.
    """
    if not items:
        return None

    from db import (
        KIT_SIZE,
        calc_shipping,
        cart_quantity_total,
        check_min_order,
        generate_payment_code,
        kit_option_available,
        product_kit_price,
    )

    shop = ensure_shop(host_chat_id)
    ok_min, _min_msg = check_min_order(shop, cart_quantity_total(items))
    if not ok_min:
        return None

    share_map = {s["product_id"]: s for s in list_shares(host_chat_id, active_only=True)}

    with get_db() as conn:
        prepared: list[dict[str, Any]] = []
        need_by_pid: dict[int, int] = {}
        for it in items:
            pid = int(it["product_id"])
            qty = int(it["quantity"])
            is_kit = bool(it.get("is_kit"))
            row = conn.execute(
                "SELECT id, chat_id, name, price, stock, active, kit_price FROM products WHERE id = ?",
                (pid,),
            ).fetchone()
            if not row or not row["active"]:
                return None

            owner = int(row["chat_id"])
            pdata = dict(row)
            if is_kit:
                kp = product_kit_price(pdata)
                if kp is None or not kit_option_available(pdata, stock=int(row["stock"])):
                    return None
                if qty % int(KIT_SIZE) != 0 or qty < int(KIT_SIZE):
                    return None
            need_by_pid[pid] = need_by_pid.get(pid, 0) + qty

            if owner == int(host_chat_id):
                if is_kit:
                    base = float(product_kit_price(pdata)) / float(KIT_SIZE)
                    sell = base
                    name = f"{row['name']} (kit of {KIT_SIZE})"
                else:
                    base = float(row["price"])
                    sell = base
                    name = row["name"]
                mk = 0.0
                is_guest = 0
            else:
                sh = share_map.get(pid)
                if not sh or int(sh["guest_chat_id"]) != owner:
                    return None  # not shared to this host
                mk = float(sh["markup_pct"])
                if is_kit:
                    base = float(product_kit_price(pdata)) / float(KIT_SIZE)
                    name = f"{row['name']} (kit of {KIT_SIZE})"
                else:
                    base = float(row["price"])
                    name = row["name"]
                sell = round(base * (1 + mk / 100.0), 2)
                is_guest = 1

            prepared.append(
                {
                    "product_id": pid,
                    "product_name": name,
                    "unit_price": sell,
                    "base_unit_price": base,
                    "markup_pct": mk,
                    "quantity": qty,
                    "owner_chat_id": owner,
                    "is_guest": is_guest,
                    "line_total": round(sell * qty, 2),
                }
            )

        for pid, need in need_by_pid.items():
            row = conn.execute(
                "SELECT stock FROM products WHERE id = ?", (pid,)
            ).fetchone()
            have = int(row["stock"]) if row else 0
            if have < need:
                return None

        subtotal = sum(p["line_total"] for p in prepared)
        try:
            from franchise import customer_shipping_total, ensure_franchise_tables

            ensure_franchise_tables()
            shipping, hidden_fee = customer_shipping_total(shop, subtotal)
        except Exception:
            shipping = calc_shipping(shop, subtotal)
            hidden_fee = 0.0
        total = subtotal + shipping
        now = _utc_now()
        pm_id = payment_method["id"] if payment_method else None
        pm_name = payment_method["name"] if payment_method else None

        cur = conn.execute(
            """
            INSERT INTO orders (
                chat_id, user_id, username, full_name, status,
                subtotal, shipping_fee, total,
                payment_method_id, payment_method_name,
                ship_name, ship_address, ship_notes,
                created_at, updated_at, hidden_service_fee
            ) VALUES (?, ?, ?, ?, 'pending_payment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                host_chat_id,
                user_id,
                username,
                full_name,
                subtotal,
                shipping,
                total,
                pm_id,
                pm_name,
                ship_name,
                ship_address,
                ship_notes,
                now,
                now,
                float(hidden_fee),
            ),
        )
        order_id = int(cur.lastrowid)

        payment_code = generate_payment_code(order_id)
        try:
            conn.execute(
                "UPDATE orders SET payment_code = ? WHERE id = ?",
                (payment_code, order_id),
            )
        except Exception:
            payment_code = f"UF{order_id}-X"
            conn.execute(
                "UPDATE orders SET payment_code = ? WHERE id = ?",
                (payment_code, order_id),
            )

        for p in prepared:
            conn.execute(
                """
                INSERT INTO order_items
                  (order_id, product_id, product_name, unit_price, quantity, line_total,
                   owner_chat_id, base_unit_price, markup_pct, is_guest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    p["product_id"],
                    p["product_name"],
                    p["unit_price"],
                    p["quantity"],
                    p["line_total"],
                    p["owner_chat_id"],
                    p["base_unit_price"],
                    p["markup_pct"],
                    p["is_guest"],
                ),
            )

        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row)


def confirm_payment_multi(
    order_id: int,
    admin_id: int,
    *,
    tracking_number: str | None = None,
    tracking_carrier: str | None = None,
) -> tuple[bool, str, list[dict]]:
    """Confirm payment, deduct per-owner stock, create guest settlements (base cost)."""
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not order:
            return False, "Order not found.", []
        if order["status"] == "paid":
            return False, "Order already paid.", []
        if order["status"] in ("cancelled", "rejected"):
            return False, f"Order is {order['status']}.", []
        if order["status"] not in ("pending_payment", "awaiting_confirmation"):
            return False, f"Cannot confirm order in status: {order['status']}", []

        items = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()

        for it in items:
            if it["product_id"] is None:
                continue
            prod = conn.execute(
                "SELECT stock, name FROM products WHERE id = ?",
                (it["product_id"],),
            ).fetchone()
            if not prod:
                return False, f"Product missing: {it['product_name']}", []
            if int(prod["stock"]) < int(it["quantity"]):
                return (
                    False,
                    f"Insufficient stock for {prod['name']}: "
                    f"need {it['quantity']}, have {prod['stock']}.",
                    [],
                )

        host_shop = conn.execute(
            "SELECT * FROM shops WHERE chat_id = ?", (order["chat_id"],)
        ).fetchone()
        display = shop_display(dict(host_shop) if host_shop else None)
        threshold = int(display["low_stock_threshold"])
        low_stock_alerts: list[dict] = []

        # Deduct
        guest_amounts: dict[int, float] = {}
        for it in items:
            if it["product_id"] is None:
                continue
            prod = conn.execute(
                "SELECT id, chat_id, name, stock FROM products WHERE id = ?",
                (it["product_id"],),
            ).fetchone()
            if not prod:
                continue
            qty = int(it["quantity"])
            before = int(prod["stock"])
            after = before - qty
            conn.execute(
                "UPDATE products SET stock = stock - ?, updated_at = ? WHERE id = ?",
                (qty, _utc_now(), it["product_id"]),
            )
            _insert_audit(
                conn,
                chat_id=int(prod["chat_id"]),
                product_id=int(prod["id"]),
                product_name=prod["name"],
                delta=-qty,
                stock_before=before,
                stock_after=after,
                reason="order_paid_confirm",
                actor_id=admin_id,
                order_id=order_id,
            )
            if after <= threshold:
                low_stock_alerts.append(
                    {
                        "product_id": int(prod["id"]),
                        "name": prod["name"],
                        "stock": after,
                        "threshold": threshold,
                        "chat_id": int(prod["chat_id"]),
                    }
                )

            owner = int(it["owner_chat_id"] or prod["chat_id"])
            if owner != int(order["chat_id"]):
                # Guest settlement = base cost × qty (host keeps markup)
                base = float(it["base_unit_price"] if it["base_unit_price"] is not None else it["unit_price"])
                guest_amounts[owner] = guest_amounts.get(owner, 0.0) + base * qty

        now = _utc_now()
        tn = (tracking_number or "").strip()
        if tn == "-":
            tn = ""
        car = (tracking_carrier or "").strip() or None
        if tn:
            conn.execute(
                """
                UPDATE orders
                SET status = 'paid', confirmed_by = ?, paid_at = ?, updated_at = ?,
                    tracking_number = ?, tracking_carrier = ?, shipped_at = ?
                WHERE id = ?
                """,
                (admin_id, now, now, tn, car, now, order_id),
            )
        else:
            conn.execute(
                """
                UPDATE orders
                SET status = 'paid', confirmed_by = ?, paid_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (admin_id, now, now, order_id),
            )

        for guest_id, amount in guest_amounts.items():
            conn.execute(
                """
                INSERT INTO order_settlements
                  (order_id, host_chat_id, guest_chat_id, amount, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'owed', ?, ?)
                """,
                (
                    order_id,
                    int(order["chat_id"]),
                    guest_id,
                    round(amount, 2),
                    now,
                    now,
                ),
            )

        return True, "Payment confirmed. Inventory updated. Guest settlements recorded.", low_stock_alerts


def list_settlements(chat_id: int, *, as_host: bool = True, status: str | None = "owed") -> list[dict]:
    with get_db() as conn:
        if as_host:
            q = """
                SELECT st.*, gs.title AS guest_title, o.payment_code
                FROM order_settlements st
                LEFT JOIN shops gs ON gs.chat_id = st.guest_chat_id
                LEFT JOIN orders o ON o.id = st.order_id
                WHERE st.host_chat_id = ?
            """
            args: list[Any] = [chat_id]
        else:
            q = """
                SELECT st.*, hs.title AS host_title, o.payment_code
                FROM order_settlements st
                LEFT JOIN shops hs ON hs.chat_id = st.host_chat_id
                LEFT JOIN orders o ON o.id = st.order_id
                WHERE st.guest_chat_id = ?
            """
            args = [chat_id]
        if status:
            q += " AND st.status = ?"
            args.append(status)
        q += " ORDER BY st.id DESC LIMIT 50"
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def mark_settlement_paid(settlement_id: int, host_admin_id: int, note: str = "") -> tuple[bool, str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM order_settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
        if not row:
            return False, "Settlement not found."
        if not is_admin(int(row["host_chat_id"]), host_admin_id):
            return False, "Host admin only."
        if row["status"] == "paid":
            return False, "Already marked paid."
        now = _utc_now()
        conn.execute(
            """
            UPDATE order_settlements
            SET status = 'paid', paid_at = ?, note = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, note or f"Paid by admin {host_admin_id}", now, settlement_id),
        )
        return True, "Marked paid to guest shop."
