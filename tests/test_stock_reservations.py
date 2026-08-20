"""Checkout stock reservations: soft hold, expire, 409 when oversold."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


SHOP = 44001
BUYER = 55001


class StockReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "reserve.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Reserve Shop")
        self.pid = db.add_product(SHOP, "SEMA 5mg", 55.0, stock=5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _order(self, qty: int, *, user_id: int = BUYER) -> dict | None:
        return db.create_order(
            SHOP,
            user_id,
            "buyer",
            "Buyer",
            [
                {
                    "product_id": self.pid,
                    "product_name": "SEMA 5mg",
                    "unit_price": 55.0,
                    "quantity": qty,
                }
            ],
            {"id": None, "name": "Venmo"},
            "Buyer",
            "1 Test St",
        )

    def test_create_order_does_not_deduct_stock(self) -> None:
        o = self._order(2)
        self.assertIsNotNone(o)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 5)
        holds = db.list_order_reservations(int(o["id"]))
        self.assertEqual(len(holds), 1)
        self.assertEqual(int(holds[0]["quantity"]), 2)
        self.assertIsNone(holds[0]["released_at"])
        self.assertEqual(db.available_to_sell(5, self.pid), 3)

    def test_second_order_409_when_reserved(self) -> None:
        first = self._order(4)
        self.assertIsNotNone(first)
        second = self._order(2, user_id=BUYER + 1)
        self.assertIsNone(second)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 5)
        self.assertEqual(db.available_to_sell(5, self.pid), 1)

    def test_expire_releases_then_order_succeeds(self) -> None:
        o = self._order(5)
        self.assertIsNotNone(o)
        self.assertIsNone(self._order(1, user_id=BUYER + 1))
        with db.get_db() as conn:
            conn.execute(
                "UPDATE stock_reservations SET expires_at = ? WHERE order_id = ?",
                ("2000-01-01 00:00:00", int(o["id"])),
            )
        n = db.expire_stale_reservations("2000-01-01 00:00:01")
        self.assertGreaterEqual(n, 1)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 5)
        later = self._order(3, user_id=BUYER + 2)
        self.assertIsNotNone(later)
        holds = db.list_order_reservations(int(o["id"]))
        self.assertTrue(all(h.get("released_at") for h in holds))
        self.assertEqual(holds[0]["release_reason"], "expired")

    def test_confirm_deducts_and_releases_hold(self) -> None:
        o = self._order(2)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 5)
        ok, msg, _ = db.confirm_order_payment(int(o["id"]), 99)
        self.assertTrue(ok, msg)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 3)
        holds = db.list_order_reservations(int(o["id"]))
        self.assertTrue(all(h.get("released_at") for h in holds))
        self.assertEqual(holds[0]["release_reason"], "confirmed")

    def test_cancel_releases_without_stock_change(self) -> None:
        o = self._order(3)
        ok, msg = db.cancel_order(int(o["id"]), BUYER)
        self.assertTrue(ok, msg)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 5)
        holds = db.list_order_reservations(int(o["id"]))
        self.assertTrue(all(h.get("released_at") for h in holds))
        other = self._order(5, user_id=BUYER + 3)
        self.assertIsNotNone(other)

    def test_legacy_order_without_reservation_still_confirms(self) -> None:
        o = self._order(1)
        with db.get_db() as conn:
            conn.execute(
                "DELETE FROM stock_reservations WHERE order_id = ?",
                (int(o["id"]),),
            )
        self.assertEqual(db.list_order_reservations(int(o["id"])), [])
        ok, msg, _ = db.confirm_order_payment(int(o["id"]), 99)
        self.assertTrue(ok, msg)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 4)


if __name__ == "__main__":
    unittest.main()
