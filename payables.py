"""What SPBC owes each vendor.

The money path is customer → franchisee → SPBC → vendor. The first three hops
are recorded (orders, invoices, service fees); the last was not. When a vendor
fulfilled an order their stock moved and the margin was shown once in a DM,
then it was gone — so "what do I owe Unicorn this week" meant scrolling chat.

Every accepted fulfillment now writes one line here: the order, the vendor,
what they are owed, what the order brought in, and the resulting margin.
Lines stay `open` until marked settled, so /owed is always the current answer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import db

log = logging.getLogger("payables")

OPEN = "open"
SETTLED = "settled"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables() -> None:
    with db.get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_payables (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number  TEXT NOT NULL,
                shop_chat_id  INTEGER NOT NULL,
                shop_title    TEXT,
                amount        REAL NOT NULL,
                order_total   REAL NOT NULL DEFAULT 0,
                margin        REAL NOT NULL DEFAULT 0,
                lines         TEXT,
                status        TEXT NOT NULL DEFAULT 'open',
                created_at    TEXT NOT NULL,
                settled_at    TEXT,
                settled_by    INTEGER,
                UNIQUE (order_number, shop_chat_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payables_shop_status "
            "ON vendor_payables(shop_chat_id, status)"
        )


def record(quote: dict) -> Optional[int]:
    """Log what we owe a vendor for an accepted fulfillment. Idempotent."""
    ensure_tables()
    try:
        amount = round(float(quote.get("total") or 0), 2)
        order_total = round(float(quote.get("order_total") or 0), 2)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    summary = ", ".join(
        f"{ln.get('qty')}× {ln.get('name')}" for ln in (quote.get("lines") or [])
    )[:500]
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO vendor_payables
                (order_number, shop_chat_id, shop_title, amount, order_total,
                 margin, lines, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            ON CONFLICT(order_number, shop_chat_id) DO NOTHING
            """,
            (
                str(quote.get("order_number") or "?"),
                int(quote.get("shop_chat_id") or 0),
                str(quote.get("shop_title") or ""),
                amount,
                order_total,
                round(order_total - amount, 2) if order_total else 0.0,
                summary,
                _utc_now(),
            ),
        )
        return int(cur.lastrowid) if cur.rowcount else None


def open_totals() -> list[dict]:
    """Per-vendor outstanding balance, biggest first."""
    ensure_tables()
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT shop_chat_id, shop_title,
                   COUNT(*) AS orders,
                   ROUND(SUM(amount), 2) AS owed,
                   ROUND(SUM(margin), 2) AS margin
            FROM vendor_payables
            WHERE status = 'open'
            GROUP BY shop_chat_id, shop_title
            ORDER BY owed DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def open_for_shop(shop_chat_id: int, limit: int = 25) -> list[dict]:
    ensure_tables()
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM vendor_payables WHERE shop_chat_id = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT ?",
            (int(shop_chat_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def settle_shop(shop_chat_id: int, actor_id: int) -> tuple[int, float]:
    """Mark everything outstanding for a vendor as paid. Returns (count, total)."""
    ensure_tables()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(amount),0),2) AS total "
            "FROM vendor_payables WHERE shop_chat_id = ? AND status = 'open'",
            (int(shop_chat_id),),
        ).fetchone()
        n, total = int(row["n"]), float(row["total"])
        if n:
            conn.execute(
                "UPDATE vendor_payables SET status = 'settled', settled_at = ?, "
                "settled_by = ? WHERE shop_chat_id = ? AND status = 'open'",
                (_utc_now(), int(actor_id), int(shop_chat_id)),
            )
    log.info("settled %s payable(s) for shop %s totalling %s", n, shop_chat_id, total)
    return n, total


def summary_text(currency: str = "$") -> str:
    """The /owed message."""
    totals = open_totals()
    if not totals:
        return "💸 *Vendor payables*\n\nNothing outstanding — you're square."
    lines = ["💸 *Vendor payables* — what you owe right now", ""]
    grand = 0.0
    margin = 0.0
    for t in totals:
        grand += float(t["owed"] or 0)
        margin += float(t["margin"] or 0)
        lines.append(
            f"• {t['shop_title'] or t['shop_chat_id']} — "
            f"*{currency}{float(t['owed'] or 0):.2f}* across {t['orders']} order"
            f"{'s' if int(t['orders']) != 1 else ''}"
        )
    lines += [
        "",
        f"Total owed: *{currency}{grand:.2f}*",
    ]
    if margin:
        lines.append(f"Your margin on those orders: {currency}{margin:.2f}")
    lines.append("\nTap a vendor below once you've paid them.")
    return "\n".join(lines)
