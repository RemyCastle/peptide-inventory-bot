"""SQLite storage for supplier watch. Single writer (the watcher process).

Raw messages are always stored, even when parsing fails — bad parses must
never poison price history, so price_points are only written on a
successful parse and diffs only compare successful rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    chat_title TEXT,
    msg_id INTEGER NOT NULL,
    msg_date TEXT NOT NULL,
    text TEXT NOT NULL,
    parse_status TEXT NOT NULL DEFAULT 'pending',  -- pending|llm_ok|regex_ok|no_prices|failed
    created_at TEXT NOT NULL,
    UNIQUE (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS price_points (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    supplier TEXT NOT NULL,
    product_key TEXT NOT NULL,     -- normalized: BPC157|5MG
    product TEXT NOT NULL,         -- display name as parsed
    size TEXT,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    raw_message_id INTEGER REFERENCES raw_messages(id),
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pp_lookup ON price_points (chat_id, product_key, seen_at);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_raw(conn, chat_id: int, chat_title: str, msg_id: int,
             msg_date: str, text: str) -> int | None:
    """Insert a raw message; returns row id, or None if already seen."""
    try:
        cur = conn.execute(
            "INSERT INTO raw_messages (chat_id, chat_title, msg_id, msg_date, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, chat_title, msg_id, msg_date, text, _now()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def set_parse_status(conn, raw_id: int, status: str) -> None:
    conn.execute("UPDATE raw_messages SET parse_status=? WHERE id=?", (status, raw_id))
    conn.commit()


def latest_price(conn, chat_id: int, product_key: str):
    """Most recent price row for this supplier+product, or None."""
    return conn.execute(
        "SELECT * FROM price_points WHERE chat_id=? AND product_key=? "
        "ORDER BY seen_at DESC, id DESC LIMIT 1",
        (chat_id, product_key),
    ).fetchone()


def record_price(conn, chat_id: int, supplier: str, item: dict,
                 raw_id: int) -> dict | None:
    """Append a price point. Returns an alert dict if new or changed, else None."""
    key = item["product_key"]
    prev = latest_price(conn, chat_id, key)
    conn.execute(
        "INSERT INTO price_points (chat_id, supplier, product_key, product, size, "
        "price, currency, raw_message_id, seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (chat_id, supplier, key, item["product"], item.get("size"),
         item["price"], item.get("currency", "USD"), raw_id, _now()),
    )
    conn.commit()

    if prev is None:
        return {"kind": "new", "supplier": supplier, "item": item}
    if abs(prev["price"] - item["price"]) > 1e-9 or prev["currency"] != item.get("currency", "USD"):
        return {"kind": "change", "supplier": supplier, "item": item,
                "old_price": prev["price"], "old_currency": prev["currency"]}
    return None


def cheapest_per_product(conn):
    """Latest price per (supplier, product), then min across suppliers — for digests."""
    return conn.execute(
        """
        WITH latest AS (
            SELECT chat_id, supplier, product_key, product, size, price, currency,
                   ROW_NUMBER() OVER (PARTITION BY chat_id, product_key
                                      ORDER BY seen_at DESC, id DESC) AS rn
            FROM price_points
        )
        SELECT product_key, product, size, supplier, price, currency
        FROM latest WHERE rn = 1
        ORDER BY product_key, price ASC
        """
    ).fetchall()
