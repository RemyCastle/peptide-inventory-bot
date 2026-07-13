"""SQLite data layer for multi-shop peptide inventory & orders."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable, Optional

from config import (
    BRAND_NAME,
    CURRENCY,
    CURRENCY_SYMBOL,
    DB_PATH,
    DEFAULT_FREE_SHIPPING_ABOVE,
    DEFAULT_LOW_STOCK_THRESHOLD,
    DEFAULT_SHIPPING_FEE,
    OWNER_IDS,
    SCHEMA_VERSION,
    WELCOME_TEXT,
)

_lock = threading.RLock()
_db_path: Path = Path(DB_PATH)


def set_db_path(path: Path | str) -> None:
    """Override DB path (used by tests)."""
    global _db_path
    _db_path = Path(path)


def get_db_path() -> Path:
    return _db_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or _db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def get_schema_version() -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT version FROM schema_meta WHERE id = 1"
        ).fetchone()
        return int(row["version"]) if row else 0


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shops (
                chat_id     INTEGER PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT 'Shop',
                currency    TEXT NOT NULL DEFAULT 'USD',
                shipping_enabled INTEGER NOT NULL DEFAULT 1,
                shipping_fee REAL NOT NULL DEFAULT 8.0,
                free_shipping_above REAL NOT NULL DEFAULT 150.0,
                shipping_label TEXT NOT NULL DEFAULT 'Standard shipping',
                welcome_text TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                added_by    INTEGER,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id),
                FOREIGN KEY (chat_id) REFERENCES shops(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price       REAL NOT NULL,
                stock       INTEGER NOT NULL DEFAULT 0,
                unit        TEXT NOT NULL DEFAULT 'vial',
                active      INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES shops(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payment_methods (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                name        TEXT NOT NULL,
                instructions TEXT NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES shops(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id         INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                username        TEXT,
                full_name       TEXT,
                status          TEXT NOT NULL DEFAULT 'pending_payment',
                -- pending_payment | awaiting_confirmation | paid | cancelled | rejected
                subtotal        REAL NOT NULL DEFAULT 0,
                shipping_fee    REAL NOT NULL DEFAULT 0,
                total           REAL NOT NULL DEFAULT 0,
                payment_method_id INTEGER,
                payment_method_name TEXT,
                ship_name       TEXT,
                ship_address    TEXT,
                ship_notes      TEXT,
                admin_note      TEXT,
                confirmed_by    INTEGER,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                paid_at         TEXT,
                FOREIGN KEY (chat_id) REFERENCES shops(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        INTEGER NOT NULL,
                product_id      INTEGER,
                product_name    TEXT NOT NULL,
                unit_price      REAL NOT NULL,
                quantity        INTEGER NOT NULL,
                line_total      REAL NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_products_chat ON products(chat_id);
            CREATE INDEX IF NOT EXISTS idx_products_chat_name ON products(chat_id, name);
            CREATE INDEX IF NOT EXISTS idx_orders_chat_status ON orders(chat_id, status);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_admins_user ON admins(user_id);

            CREATE TABLE IF NOT EXISTS stock_audit (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER,
                product_id    INTEGER,
                product_name  TEXT,
                delta         INTEGER NOT NULL,
                stock_before  INTEGER,
                stock_after   INTEGER,
                reason        TEXT NOT NULL DEFAULT '',
                actor_id      INTEGER,
                order_id      INTEGER,
                created_at    TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_stock_audit_product ON stock_audit(product_id);
            CREATE INDEX IF NOT EXISTS idx_stock_audit_chat ON stock_audit(chat_id);
            CREATE INDEX IF NOT EXISTS idx_stock_audit_order ON stock_audit(order_id);
            """
        )

        # Multi-tenant shop columns (instance defaults live in config/.env)
        _ensure_column(conn, "shops", "brand_name", "TEXT")
        _ensure_column(conn, "shops", "currency_symbol", "TEXT")
        _ensure_column(conn, "shops", "low_stock_threshold", "INTEGER")
        _ensure_column(conn, "shops", "setup_complete", "INTEGER NOT NULL DEFAULT 0")

        # Structured payment method fields (name/instructions remain buyer-facing)
        _ensure_column(conn, "payment_methods", "method_type", "TEXT")
        _ensure_column(conn, "payment_methods", "cashtag", "TEXT")
        _ensure_column(conn, "payment_methods", "handle", "TEXT")
        _ensure_column(conn, "payment_methods", "chain", "TEXT")
        _ensure_column(conn, "payment_methods", "address", "TEXT")
        _ensure_column(conn, "payment_methods", "network_note", "TEXT")

        # Per-product COA (Certificate of Analysis)
        _ensure_column(conn, "products", "coa_url", "TEXT")
        _ensure_column(conn, "products", "coa_file_id", "TEXT")
        _ensure_column(conn, "products", "coa_file_type", "TEXT")  # document | photo
        _ensure_column(conn, "products", "coa_filename", "TEXT")

        # Order payment ref code, proof screenshot, shipping tracking
        _ensure_column(conn, "orders", "payment_code", "TEXT")
        _ensure_column(conn, "orders", "payment_proof_file_id", "TEXT")
        _ensure_column(conn, "orders", "payment_proof_file_type", "TEXT")
        _ensure_column(conn, "orders", "tracking_number", "TEXT")
        _ensure_column(conn, "orders", "tracking_carrier", "TEXT")
        _ensure_column(conn, "orders", "shipped_at", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_code "
            "ON orders(payment_code) WHERE payment_code IS NOT NULL"
        )

        now = _utc_now()
        row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
        if row:
            conn.execute(
                "UPDATE schema_meta SET version = ?, updated_at = ? WHERE id = 1",
                (SCHEMA_VERSION, now),
            )
        else:
            conn.execute(
                "INSERT INTO schema_meta (id, version, updated_at) VALUES (1, ?, ?)",
                (SCHEMA_VERSION, now),
            )


def shop_display(shop: dict | None) -> dict[str, Any]:
    """Resolved branding for a shop with instance-level fallbacks."""
    shop = shop or {}
    threshold = shop.get("low_stock_threshold")
    if threshold is None:
        threshold = DEFAULT_LOW_STOCK_THRESHOLD
    return {
        "brand_name": (shop.get("brand_name") or BRAND_NAME).strip() or BRAND_NAME,
        "currency": (shop.get("currency") or CURRENCY).strip() or CURRENCY,
        "currency_symbol": (shop.get("currency_symbol") or CURRENCY_SYMBOL).strip()
        or CURRENCY_SYMBOL,
        "welcome_text": (shop.get("welcome_text") or WELCOME_TEXT).strip() or WELCOME_TEXT,
        "low_stock_threshold": int(threshold),
        "title": shop.get("title") or "Shop",
    }


# ── Shops ────────────────────────────────────────────────────────────────────


def ensure_shop(chat_id: int, title: str = "Shop") -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM shops WHERE chat_id = ?", (chat_id,)).fetchone()
        if row:
            return dict(row)
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO shops (
                chat_id, title, currency, shipping_fee, free_shipping_above,
                welcome_text, brand_name, currency_symbol, low_stock_threshold, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                title,
                CURRENCY,
                DEFAULT_SHIPPING_FEE,
                DEFAULT_FREE_SHIPPING_ABOVE,
                WELCOME_TEXT,
                BRAND_NAME,
                CURRENCY_SYMBOL,
                DEFAULT_LOW_STOCK_THRESHOLD,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM shops WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row)


def get_shop(chat_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM shops WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None


def update_shop(chat_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "title",
        "shipping_enabled",
        "shipping_fee",
        "free_shipping_above",
        "shipping_label",
        "welcome_text",
        "brand_name",
        "currency",
        "currency_symbol",
        "low_stock_threshold",
        "setup_complete",
    }
    cols = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k in allowed:
            cols.append(f"{k} = ?")
            vals.append(v)
    if not cols:
        return
    vals.append(chat_id)
    with get_db() as conn:
        conn.execute(f"UPDATE shops SET {', '.join(cols)} WHERE chat_id = ?", vals)


def calc_shipping(shop: dict, subtotal: float) -> float:
    if not shop.get("shipping_enabled", 1):
        return 0.0
    fee = float(shop.get("shipping_fee") or 0)
    free_above = float(shop.get("free_shipping_above") or 0)
    if free_above > 0 and subtotal >= free_above:
        return 0.0
    return max(0.0, fee)


# ── Admins ───────────────────────────────────────────────────────────────────


def is_owner(user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    # Bootstrap: if no OWNER_IDS set, first user can create the first shop;
    # after that, only existing shop admins get owner-level powers until OWNER_IDS is set.
    if not OWNER_IDS:
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM shops").fetchone()["c"]
            if int(count) == 0:
                return True
            row = conn.execute(
                "SELECT 1 FROM admins WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            return row is not None
    return False


def is_admin(chat_id: int, user_id: int) -> bool:
    if is_owner(user_id):
        return True
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return row is not None


def add_admin(chat_id: int, user_id: int, username: str | None, added_by: int) -> None:
    ensure_shop(chat_id)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO admins (chat_id, user_id, username, added_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET username = excluded.username
            """,
            (chat_id, user_id, username, added_by, _utc_now()),
        )


def remove_admin(chat_id: int, user_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM admins WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        return cur.rowcount > 0


def list_admins(chat_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM admins WHERE chat_id = ? ORDER BY created_at",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def shops_for_admin(user_id: int) -> list[dict]:
    """Shops this user can admin (plus all shops if owner)."""
    with get_db() as conn:
        if is_owner(user_id):
            rows = conn.execute("SELECT * FROM shops ORDER BY title").fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.* FROM shops s
                JOIN admins a ON a.chat_id = s.chat_id
                WHERE a.user_id = ?
                ORDER BY s.title
                """,
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


# ── Products ─────────────────────────────────────────────────────────────────


def add_product(
    chat_id: int,
    name: str,
    price: float,
    stock: int = 0,
    description: str = "",
    unit: str = "vial",
) -> int:
    ensure_shop(chat_id)
    now = _utc_now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO products
              (chat_id, name, description, price, stock, unit, active, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
            """,
            (chat_id, name.strip(), description.strip(), float(price), int(stock), unit, now, now),
        )
        return int(cur.lastrowid)


def update_product(product_id: int, **fields: Any) -> bool:
    allowed = {"name", "description", "price", "stock", "unit", "active", "sort_order"}
    cols = ["updated_at = ?"]
    vals: list[Any] = [_utc_now()]
    for k, v in fields.items():
        if k in allowed:
            cols.append(f"{k} = ?")
            vals.append(v)
    vals.append(product_id)
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE products SET {', '.join(cols)} WHERE id = ?",
            vals,
        )
        return cur.rowcount > 0


def rename_product(
    product_id: int, chat_id: int, new_name: str, *, max_len: int = 120
) -> tuple[bool, str]:
    """
    Rename a product scoped to shop (chat_id). Returns (ok, message_or_name).
    Rejects empty / whitespace-only names. Does not touch stock or price.
    """
    name = (new_name or "").strip()
    if not name:
        return False, "Name can't be empty. Send a new name or /cancel."
    if len(name) > max_len:
        return False, f"Name too long (max {max_len} characters). Try again or /cancel."
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE products
            SET name = ?, updated_at = ?
            WHERE id = ? AND chat_id = ?
            """,
            (name, _utc_now(), product_id, chat_id),
        )
        if cur.rowcount <= 0:
            return False, "Product not found in this shop."
    return True, name


def is_valid_coa_url(url: str) -> bool:
    """Simple validation: http(s) URL, no whitespace."""
    u = (url or "").strip()
    if not u or any(c.isspace() for c in u):
        return False
    low = u.lower()
    return low.startswith("http://") or low.startswith("https://")


def product_has_coa_file(p: dict | None) -> bool:
    """True if product has a Telegram-stored COA file (PDF/photo)."""
    if not p:
        return False
    return bool((p.get("coa_file_id") or "").strip())


def product_has_coa_url(p: dict | None) -> bool:
    """True if product has an external COA URL."""
    if not p:
        return False
    return bool((p.get("coa_url") or "").strip())


def product_has_coa(p: dict | None) -> bool:
    """True if product has a COA file and/or external link."""
    return product_has_coa_file(p) or product_has_coa_url(p)


def set_product_coa_url(
    product_id: int, chat_id: int, url: str
) -> tuple[bool, str]:
    """
    Set COA URL for a product in a shop. Returns (ok, message_or_url).
    Prefer set_product_coa_file for buyer-facing COA delivery.
    """
    u = (url or "").strip()
    if not is_valid_coa_url(u):
        return (
            False,
            "Invalid link. Send a full URL starting with http:// or https:// (no spaces).",
        )
    if len(u) > 2000:
        return False, "URL too long. Try a shorter share link."
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE products
            SET coa_url = ?, updated_at = ?
            WHERE id = ? AND chat_id = ?
            """,
            (u, _utc_now(), product_id, chat_id),
        )
        if cur.rowcount <= 0:
            return False, "Product not found in this shop."
    return True, u


def set_product_coa_file(
    product_id: int,
    chat_id: int,
    file_id: str,
    file_type: str,
    filename: str | None = None,
) -> tuple[bool, str]:
    """
    Store Telegram file_id for COA (document PDF or photo).
    file_type: 'document' | 'photo'
    """
    fid = (file_id or "").strip()
    ftype = (file_type or "").strip().lower()
    if not fid:
        return False, "Missing file."
    if ftype not in ("document", "photo"):
        return False, "File type must be document or photo."
    fname = (filename or "").strip() or None
    if fname and len(fname) > 255:
        fname = fname[:255]
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE products
            SET coa_file_id = ?,
                coa_file_type = ?,
                coa_filename = ?,
                updated_at = ?
            WHERE id = ? AND chat_id = ?
            """,
            (fid, ftype, fname, _utc_now(), product_id, chat_id),
        )
        if cur.rowcount <= 0:
            return False, "Product not found in this shop."
    return True, ftype


def clear_product_coa(product_id: int, chat_id: int) -> bool:
    """Clear all COA fields (file + URL)."""
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE products
            SET coa_url = NULL,
                coa_file_id = NULL,
                coa_file_type = NULL,
                coa_filename = NULL,
                updated_at = ?
            WHERE id = ? AND chat_id = ?
            """,
            (_utc_now(), product_id, chat_id),
        )
        return cur.rowcount > 0


def clear_product_coa_url(product_id: int, chat_id: int) -> bool:
    """Back-compat alias: clears full COA (file + URL)."""
    return clear_product_coa(product_id, chat_id)


def _insert_audit(
    conn: sqlite3.Connection,
    *,
    chat_id: int | None,
    product_id: int | None,
    product_name: str | None,
    delta: int,
    stock_before: int | None,
    stock_after: int | None,
    reason: str,
    actor_id: int | None = None,
    order_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO stock_audit (
            chat_id, product_id, product_name, delta,
            stock_before, stock_after, reason, actor_id, order_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            product_id,
            product_name,
            int(delta),
            stock_before,
            stock_after,
            reason,
            actor_id,
            order_id,
            _utc_now(),
        ),
    )


def adjust_stock(
    product_id: int,
    delta: int,
    *,
    actor_id: int | None = None,
    reason: str = "manual_adjust",
    order_id: int | None = None,
) -> Optional[int]:
    """Adjust stock by delta. Returns new stock or None if product missing."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, chat_id, name, stock FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if not row:
            return None
        before = int(row["stock"])
        new_stock = max(0, before + int(delta))
        applied = new_stock - before
        conn.execute(
            "UPDATE products SET stock = ?, updated_at = ? WHERE id = ?",
            (new_stock, _utc_now(), product_id),
        )
        if applied != 0:
            _insert_audit(
                conn,
                chat_id=int(row["chat_id"]),
                product_id=product_id,
                product_name=row["name"],
                delta=applied,
                stock_before=before,
                stock_after=new_stock,
                reason=reason,
                actor_id=actor_id,
                order_id=order_id,
            )
        return new_stock


def list_stock_audit(
    chat_id: int | None = None,
    product_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    with get_db() as conn:
        if product_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM stock_audit
                WHERE product_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (product_id, limit),
            ).fetchall()
        elif chat_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM stock_audit
                WHERE chat_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_product(product_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None


def list_products(chat_id: int, active_only: bool = False) -> list[dict]:
    with get_db() as conn:
        if active_only:
            rows = conn.execute(
                """
                SELECT * FROM products
                WHERE chat_id = ? AND active = 1
                ORDER BY sort_order, name
                """,
                (chat_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM products
                WHERE chat_id = ?
                ORDER BY active DESC, sort_order, name
                """,
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def search_products(
    chat_id: int,
    query: str,
    *,
    active_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """
    Per-shop product search (name + description).
    Mirrors catalog: active products only when active_only=True (includes out-of-stock).
    Case-insensitive partial match. Scoped to chat_id only (no cross-shop).
    """
    q = (query or "").strip()
    if not q:
        return []
    pattern = f"%{q}%"
    with get_db() as conn:
        if active_only:
            rows = conn.execute(
                """
                SELECT * FROM products
                WHERE chat_id = ?
                  AND active = 1
                  AND (
                    name LIKE ? COLLATE NOCASE
                    OR description LIKE ? COLLATE NOCASE
                  )
                ORDER BY sort_order, name
                LIMIT ?
                """,
                (chat_id, pattern, pattern, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM products
                WHERE chat_id = ?
                  AND (
                    name LIKE ? COLLATE NOCASE
                    OR description LIKE ? COLLATE NOCASE
                  )
                ORDER BY active DESC, sort_order, name
                LIMIT ?
                """,
                (chat_id, pattern, pattern, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]


def delete_product(product_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return cur.rowcount > 0


# ── Payment methods ──────────────────────────────────────────────────────────


def add_payment_method(
    chat_id: int,
    name: str,
    instructions: str,
    *,
    method_type: str | None = None,
    cashtag: str | None = None,
    handle: str | None = None,
    chain: str | None = None,
    address: str | None = None,
    network_note: str | None = None,
) -> int:
    ensure_shop(chat_id)
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO payment_methods (
                chat_id, name, instructions, active, sort_order, created_at,
                method_type, cashtag, handle, chain, address, network_note
            )
            VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                name.strip(),
                instructions.strip(),
                _utc_now(),
                method_type,
                cashtag,
                handle,
                chain,
                address,
                network_note,
            ),
        )
        return int(cur.lastrowid)


def add_payment_from_template(chat_id: int, payload: dict[str, Any]) -> int:
    """Insert a payment method from payment_templates.render_* output."""
    return add_payment_method(
        chat_id,
        name=str(payload.get("name") or "Payment"),
        instructions=str(payload.get("instructions") or ""),
        method_type=payload.get("method_type"),
        cashtag=payload.get("cashtag"),
        handle=payload.get("handle"),
        chain=payload.get("chain"),
        address=payload.get("address"),
        network_note=payload.get("network_note"),
    )


def update_payment_method(method_id: int, **fields: Any) -> bool:
    allowed = {
        "name",
        "instructions",
        "active",
        "sort_order",
        "method_type",
        "cashtag",
        "handle",
        "chain",
        "address",
        "network_note",
    }
    cols = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k in allowed:
            cols.append(f"{k} = ?")
            vals.append(v)
    if not cols:
        return False
    vals.append(method_id)
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE payment_methods SET {', '.join(cols)} WHERE id = ?",
            vals,
        )
        return cur.rowcount > 0


def list_payment_methods(chat_id: int, active_only: bool = True) -> list[dict]:
    with get_db() as conn:
        if active_only:
            rows = conn.execute(
                """
                SELECT * FROM payment_methods
                WHERE chat_id = ? AND active = 1
                ORDER BY sort_order, name
                """,
                (chat_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM payment_methods
                WHERE chat_id = ?
                ORDER BY active DESC, sort_order, name
                """,
                (chat_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_payment_method(method_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM payment_methods WHERE id = ?", (method_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_payment_method(method_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM payment_methods WHERE id = ?", (method_id,))
        return cur.rowcount > 0


# ── Orders ───────────────────────────────────────────────────────────────────


def generate_payment_code(order_id: int | None = None) -> str:
    """
    Short unique code for payment app notes/memos.
    Format: UF-XXXXXX (uppercase alnum, easy to type).
    """
    import secrets
    import string

    alphabet = string.ascii_uppercase + string.digits
    # Avoid ambiguous 0/O, 1/I
    alphabet = alphabet.replace("0", "").replace("O", "").replace("1", "").replace("I", "")
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    if order_id is not None:
        return f"UF{int(order_id)}-{suffix}"
    return f"UF-{suffix}"


def create_order(
    chat_id: int,
    user_id: int,
    username: str | None,
    full_name: str | None,
    items: list[dict],
    # items: [{product_id, product_name, unit_price, quantity}]
    payment_method: dict | None,
    ship_name: str,
    ship_address: str,
    ship_notes: str = "",
) -> Optional[dict]:
    """
    Create an order. Does NOT deduct stock.
    Validates stock availability at creation time (snapshot check).
    Assigns a unique payment_code for memo/notes matching.
    Returns order dict or None if stock insufficient / empty cart.
    """
    if not items:
        return None

    shop = ensure_shop(chat_id)
    with get_db() as conn:
        # Re-check live stock
        for it in items:
            pid = it["product_id"]
            qty = int(it["quantity"])
            row = conn.execute(
                "SELECT id, name, price, stock, active FROM products WHERE id = ? AND chat_id = ?",
                (pid, chat_id),
            ).fetchone()
            if not row or not row["active"]:
                return None
            if int(row["stock"]) < qty:
                return None
            it["product_name"] = row["name"]
            it["unit_price"] = float(row["price"])

        subtotal = sum(float(it["unit_price"]) * int(it["quantity"]) for it in items)
        shipping = calc_shipping(shop, subtotal)
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
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending_payment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
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
            ),
        )
        order_id = int(cur.lastrowid)

        # Unique payment memo code (retry on rare collision)
        payment_code = None
        for _ in range(8):
            candidate = generate_payment_code(order_id)
            try:
                conn.execute(
                    "UPDATE orders SET payment_code = ? WHERE id = ?",
                    (candidate, order_id),
                )
                payment_code = candidate
                break
            except Exception:
                continue
        if payment_code is None:
            payment_code = f"UF{order_id}-{order_id:04d}"
            conn.execute(
                "UPDATE orders SET payment_code = ? WHERE id = ?",
                (payment_code, order_id),
            )

        for it in items:
            line = float(it["unit_price"]) * int(it["quantity"])
            conn.execute(
                """
                INSERT INTO order_items
                  (order_id, product_id, product_name, unit_price, quantity, line_total)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    it["product_id"],
                    it["product_name"],
                    float(it["unit_price"]),
                    int(it["quantity"]),
                    line,
                ),
            )

        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row)


def get_order(order_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def get_order_items(order_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_orders(
    chat_id: int,
    status: str | None = None,
    limit: int = 30,
) -> list[dict]:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE chat_id = ? AND status = ?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE chat_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def list_user_orders(user_id: int, limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_order_awaiting_confirmation(
    order_id: int,
    *,
    proof_file_id: str | None = None,
    proof_file_type: str | None = None,
) -> bool:
    """Move pending → awaiting_confirmation; optionally attach payment screenshot."""
    with get_db() as conn:
        order = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not order:
            return False
        if order["status"] not in ("pending_payment", "awaiting_confirmation"):
            return False
        now = _utc_now()
        if proof_file_id:
            ftype = (proof_file_type or "photo").strip().lower()
            if ftype not in ("photo", "document"):
                ftype = "photo"
            conn.execute(
                """
                UPDATE orders
                SET status = 'awaiting_confirmation',
                    payment_proof_file_id = ?,
                    payment_proof_file_type = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (proof_file_id.strip(), ftype, now, order_id),
            )
        else:
            conn.execute(
                """
                UPDATE orders
                SET status = 'awaiting_confirmation', updated_at = ?
                WHERE id = ?
                """,
                (now, order_id),
            )
        return True


def set_order_tracking(
    order_id: int,
    tracking_number: str,
    carrier: str | None = None,
) -> bool:
    """Set tracking on an order (usually after payment confirm)."""
    tn = (tracking_number or "").strip()
    if not tn or tn == "-":
        return False
    car = (carrier or "").strip() or None
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE orders
            SET tracking_number = ?,
                tracking_carrier = ?,
                shipped_at = COALESCE(shipped_at, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (tn, car, _utc_now(), _utc_now(), order_id),
        )
        return cur.rowcount > 0


def confirm_order_payment(
    order_id: int,
    admin_id: int,
    *,
    tracking_number: str | None = None,
    tracking_carrier: str | None = None,
) -> tuple[bool, str, list[dict]]:
    """
    Confirm payment and deduct inventory atomically.
    Optional tracking_number is saved with the paid order.
    Returns (ok, message, low_stock_alerts).
    """
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

        # Verify stock still available
        for it in items:
            if it["product_id"] is None:
                continue
            prod = conn.execute(
                "SELECT stock, name FROM products WHERE id = ?",
                (it["product_id"],),
            ).fetchone()
            if not prod:
                return False, f"Product missing for line: {it['product_name']}", []
            if int(prod["stock"]) < int(it["quantity"]):
                return (
                    False,
                    f"Insufficient stock for {prod['name']}: "
                    f"need {it['quantity']}, have {prod['stock']}.",
                    [],
                )

        shop = conn.execute(
            "SELECT * FROM shops WHERE chat_id = ?", (order["chat_id"],)
        ).fetchone()
        display = shop_display(dict(shop) if shop else None)
        threshold = int(display["low_stock_threshold"])

        low_stock_alerts: list[dict] = []

        # Deduct stock + audit
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
                """
                UPDATE products
                SET stock = stock - ?, updated_at = ?
                WHERE id = ?
                """,
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
        return True, "Payment confirmed. Inventory updated.", low_stock_alerts


def reject_order(order_id: int, admin_id: int, note: str = "") -> tuple[bool, str]:
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not order:
            return False, "Order not found."
        if order["status"] == "paid":
            return False, "Cannot reject a paid order."
        if order["status"] in ("cancelled", "rejected"):
            return False, f"Order already {order['status']}."
        conn.execute(
            """
            UPDATE orders
            SET status = 'rejected', confirmed_by = ?, admin_note = ?, updated_at = ?
            WHERE id = ?
            """,
            (admin_id, note, _utc_now(), order_id),
        )
        return True, "Order rejected. Inventory unchanged."


def cancel_order(order_id: int, user_id: int | None = None) -> tuple[bool, str]:
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not order:
            return False, "Order not found."
        if user_id is not None and order["user_id"] != user_id:
            return False, "Not your order."
        if order["status"] == "paid":
            return False, "Paid orders cannot be cancelled."
        if order["status"] in ("cancelled", "rejected"):
            return False, f"Order already {order['status']}."
        conn.execute(
            """
            UPDATE orders SET status = 'cancelled', updated_at = ?
            WHERE id = ?
            """,
            (_utc_now(), order_id),
        )
        return True, "Order cancelled. Inventory unchanged."


# ── Formatting helpers ───────────────────────────────────────────────────────


def money(amount: float, symbol: str = CURRENCY_SYMBOL) -> str:
    return f"{symbol}{amount:,.2f}"


def format_product_line(p: dict, symbol: str = CURRENCY_SYMBOL) -> str:
    stock = int(p["stock"])
    stock_txt = f"{stock} in stock" if stock > 0 else "OUT OF STOCK"
    desc = f"\n   {p['description']}" if p.get("description") else ""
    return (
        f"• *{p['name']}* — {money(float(p['price']), symbol)} / {p.get('unit') or 'vial'}\n"
        f"   _{stock_txt}_{desc}"
    )


def format_order_summary(order: dict, items: list[dict], symbol: str = CURRENCY_SYMBOL) -> str:
    lines = [
        f"*Order #{order['id']}* — `{order['status']}`",
        f"Customer: {order.get('full_name') or '—'} (@{order.get('username') or 'n/a'})",
        f"User ID: `{order['user_id']}`",
        "",
        "*Items:*",
    ]
    for it in items:
        lines.append(
            f"• {it['product_name']} × {it['quantity']} "
            f"= {money(float(it['line_total']), symbol)}"
        )
    lines += [
        "",
        f"Subtotal: {money(float(order['subtotal']), symbol)}",
        f"Shipping: {money(float(order['shipping_fee']), symbol)}",
        f"*Total: {money(float(order['total']), symbol)}*",
        f"Payment: {order.get('payment_method_name') or '—'}",
    ]
    code = (order.get("payment_code") or "").strip()
    if code:
        lines.append(f"*Payment code (memo):* `{code}`")
    if order.get("payment_proof_file_id"):
        lines.append("Payment proof: ✅ screenshot on file")
    lines += [
        "",
        f"*Ship to:* {order.get('ship_name') or '—'}",
        order.get("ship_address") or "—",
    ]
    if order.get("ship_notes"):
        lines.append(f"Notes: {order['ship_notes']}")
    track = (order.get("tracking_number") or "").strip()
    if track:
        car = (order.get("tracking_carrier") or "").strip()
        track_line = f"*Tracking:* `{track}`"
        if car:
            track_line += f" ({car})"
        lines.append(track_line)
    lines.append(f"\nCreated: {order.get('created_at')}")
    if order.get("paid_at"):
        lines.append(f"Paid: {order['paid_at']}")
    return "\n".join(lines)
