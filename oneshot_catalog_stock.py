"""ONE-SHOT: set Unicorn Magic Factory catalog products.stock=100.

Never DELETE / DROP / replace inventory.db. Other shops are not updated.
Abort unless find_catalog_shop() is chat_id -5121165394.

Safe to run twice: a marker next to the DB skips a second apply.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
import unicorn_shop

EXPECTED_CHAT_ID = -5121165394
TARGET_STOCK = 100
MARKER_NAME = ".oneshot-catalog-stock-100"
REASON = "oneshot_stock_100"


def marker_path() -> Path:
    return Path(db.get_db_path()).resolve().parent / MARKER_NAME


def marker_exists() -> bool:
    return marker_path().is_file()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _counts(conn: Any, chat_id: int) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS sku,
            SUM(CASE WHEN stock = ? THEN 1 ELSE 0 END) AS n100,
            SUM(CASE WHEN stock = 0 THEN 1 ELSE 0 END) AS n0,
            MIN(stock) AS min_stock,
            MAX(stock) AS max_stock,
            COALESCE(SUM(stock), 0) AS sum_stock
        FROM products
        WHERE chat_id = ?
        """,
        (TARGET_STOCK, int(chat_id)),
    ).fetchone()
    return {
        "sku": int(row["sku"] or 0),
        "n100": int(row["n100"] or 0),
        "n0": int(row["n0"] or 0),
        "min_stock": int(row["min_stock"] if row["min_stock"] is not None else 0),
        "max_stock": int(row["max_stock"] if row["max_stock"] is not None else 0),
        "sum_stock": int(row["sum_stock"] or 0),
    }


def _other_fingerprint(conn: Any, chat_id: int) -> list[tuple[int, int, int, int]]:
    rows = conn.execute(
        """
        SELECT chat_id,
               COUNT(*) AS n,
               COALESCE(SUM(stock), 0) AS sum_stock,
               COALESCE(SUM(id * (stock + 1)), 0) AS mix
        FROM products
        WHERE chat_id != ?
        GROUP BY chat_id
        ORDER BY chat_id
        """,
        (int(chat_id),),
    ).fetchall()
    return [
        (int(r["chat_id"]), int(r["n"] or 0), int(r["sum_stock"] or 0), int(r["mix"] or 0))
        for r in rows
    ]


def _write_marker(payload: dict[str, Any]) -> None:
    path = marker_path()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def apply_oneshot_catalog_stock(
    *,
    target_stock: int = TARGET_STOCK,
    expected_chat_id: int = EXPECTED_CHAT_ID,
    force: bool = False,
) -> dict[str, Any]:
    """Set every products.stock row for the catalog shop. Other shops untouched."""
    db.init_db()
    target = int(target_stock)
    expected = int(expected_chat_id)
    shop = unicorn_shop.find_catalog_shop()
    if not shop:
        return {"ok": False, "skipped": "no catalog shop", "applied": 0}

    try:
        cid = int(shop["chat_id"])
    except (TypeError, ValueError, KeyError):
        return {"ok": False, "skipped": "bad catalog shop", "applied": 0}

    title = str(shop.get("title") or "")
    if cid != expected:
        return {
            "ok": False,
            "skipped": "catalog shop is not the expected Unicorn shop",
            "applied": 0,
        }
    if not unicorn_shop.shop_title_looks_unicorn(title):
        return {
            "ok": False,
            "skipped": "catalog shop title is not Unicorn Magic Factory",
            "applied": 0,
        }

    if marker_exists() and not force:
        with db.get_db() as conn:
            after = _counts(conn, cid)
            other = _other_fingerprint(conn, cid)
        return {
            "ok": True,
            "skipped": "already_applied",
            "applied": 0,
            "other_shops_unchanged": True,
            "other_shop_count": len(other),
            "sku_before": after["sku"],
            "sku_after": after["sku"],
            "n100_before": after["n100"],
            "n100_after": after["n100"],
            "n0_before": after["n0"],
            "n0_after": after["n0"],
            "min_before": after["min_stock"],
            "max_before": after["max_stock"],
            "min_after": after["min_stock"],
            "max_after": after["max_stock"],
            "sum_before": after["sum_stock"],
            "sum_after": after["sum_stock"],
        }

    now = _utc_now()
    with db.get_db() as conn:
        before = _counts(conn, cid)
        if before["sku"] <= 0:
            return {
                "ok": False,
                "skipped": "catalog shop has no products",
                "applied": 0,
                "sku_before": 0,
                "sku_after": 0,
            }
        other_before = _other_fingerprint(conn, cid)
        rows = conn.execute(
            "SELECT id, name, stock FROM products WHERE chat_id = ?",
            (cid,),
        ).fetchall()
        cur = conn.execute(
            """
            UPDATE products
            SET stock = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (target, now, cid),
        )
        applied = int(cur.rowcount or 0)
        for row in rows:
            old = int(row["stock"] or 0)
            if old == target:
                continue
            conn.execute(
                """
                INSERT INTO stock_audit (
                    chat_id, product_id, product_name, delta,
                    stock_before, stock_after, reason, actor_id, order_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    int(row["id"]),
                    row["name"],
                    target - old,
                    old,
                    target,
                    REASON,
                    None,
                    None,
                    now,
                ),
            )
        after = _counts(conn, cid)
        other_after = _other_fingerprint(conn, cid)
        if other_before != other_after:
            raise RuntimeError("oneshot aborted: other shops' stock changed")
        if after["sku"] != before["sku"]:
            raise RuntimeError("oneshot aborted: catalog product count changed")
        if after["n100"] != after["sku"] or after["min_stock"] != target:
            raise RuntimeError("oneshot aborted: catalog stock not fully set")

    result = {
        "ok": True,
        "skipped": "",
        "applied": applied,
        "other_shops_unchanged": True,
        "other_shop_count": len(other_before),
        "sku_before": before["sku"],
        "sku_after": after["sku"],
        "n100_before": before["n100"],
        "n100_after": after["n100"],
        "n0_before": before["n0"],
        "n0_after": after["n0"],
        "min_before": before["min_stock"],
        "max_before": before["max_stock"],
        "min_after": after["min_stock"],
        "max_after": after["max_stock"],
        "sum_before": before["sum_stock"],
        "sum_after": after["sum_stock"],
    }
    _write_marker(
        {
            "applied_at": now,
            "expected_chat_id": expected,
            "target_stock": target,
            "sku": after["sku"],
            "applied": applied,
        }
    )
    return result


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"ONESHOT catalog stock={TARGET_STOCK}",
        f"ok={result.get('ok')} skipped={result.get('skipped') or '-'}",
        (
            "BEFORE: "
            f"sku={result.get('sku_before')} n100={result.get('n100_before')} "
            f"n0={result.get('n0_before')} min={result.get('min_before')} "
            f"max={result.get('max_before')} sum={result.get('sum_before')}"
        ),
        (
            "AFTER:  "
            f"sku={result.get('sku_after')} n100={result.get('n100_after')} "
            f"n0={result.get('n0_after')} min={result.get('min_after')} "
            f"max={result.get('max_after')} sum={result.get('sum_after')}"
        ),
        (
            f"rows_updated={result.get('applied')} "
            f"other_shops_unchanged={result.get('other_shops_unchanged')} "
            f"other_shop_count={result.get('other_shop_count')}"
        ),
        f"marker={marker_path()}",
    ]
    return "\n".join(lines)


def main() -> int:
    result = apply_oneshot_catalog_stock()
    print(format_report(result), flush=True)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
