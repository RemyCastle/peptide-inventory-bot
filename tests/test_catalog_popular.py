"""Top-N popular catalog ranking from paid order volume."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class CatalogPopularTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "pop.db")
        db.init_db()
        self.shop = 501
        self.buyer = 9
        db.ensure_shop(self.shop, title="Pop Shop")
        db.add_admin(self.shop, 1, "a", 1)
        # Create 12 active products
        self.pids = []
        for i in range(12):
            pid = db.add_product(
                self.shop, f"Peptide {i:02d}", price=10.0 + i, stock=500
            )
            self.pids.append(pid)
        db.add_payment_method(self.shop, "Cash", "pay")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _paid_order(self, lines: list[tuple[int, int]]) -> None:
        """lines: (product_id, qty)"""
        pay = db.list_payment_methods(self.shop)[0]
        items = [
            {
                "product_id": pid,
                "product_name": f"p{pid}",
                "unit_price": 10.0,
                "quantity": qty,
            }
            for pid, qty in lines
        ]
        order = db.create_order(
            self.shop,
            self.buyer,
            "b",
            "Buyer",
            items,
            pay,
            "N",
            "Addr",
        )
        self.assertIsNotNone(order)
        ok, msg, _ = db.confirm_order_payment(int(order["id"]), 1)
        self.assertTrue(ok, msg)

    def test_sales_counts(self) -> None:
        # Peptide 5 sold 30, Peptide 2 sold 10, Peptide 0 sold 5
        self._paid_order([(self.pids[5], 20), (self.pids[2], 10)])
        self._paid_order([(self.pids[5], 10), (self.pids[0], 5)])
        sales = db.product_sales_counts(self.shop)
        self.assertEqual(sales[self.pids[5]], 30)
        self.assertEqual(sales[self.pids[2]], 10)
        self.assertEqual(sales[self.pids[0]], 5)

    def test_rank_top_10(self) -> None:
        self._paid_order([(self.pids[11], 100)])  # most popular
        self._paid_order([(self.pids[3], 50)])
        self._paid_order([(self.pids[7], 25)])
        products = db.list_products(self.shop, active_only=True)
        top = db.rank_products_by_popularity(
            products, chat_id=self.shop, limit=10
        )
        self.assertEqual(len(top), 10)
        self.assertEqual(top[0]["id"], self.pids[11])
        self.assertEqual(top[1]["id"], self.pids[3])
        self.assertEqual(top[2]["id"], self.pids[7])
        # Rest filled with remaining products (by sort_order/name among zeros)
        ids = {p["id"] for p in top}
        self.assertEqual(len(ids), 10)

    def test_no_sales_still_returns_ten(self) -> None:
        products = db.list_products(self.shop, active_only=True)
        top = db.rank_products_by_popularity(
            products, chat_id=self.shop, limit=10
        )
        self.assertEqual(len(top), 10)

    def test_pending_orders_not_counted(self) -> None:
        pay = db.list_payment_methods(self.shop)[0]
        order = db.create_order(
            self.shop,
            self.buyer,
            "b",
            "B",
            [
                {
                    "product_id": self.pids[1],
                    "product_name": "x",
                    "unit_price": 10.0,
                    "quantity": 99,
                }
            ],
            pay,
            "N",
            "A",
        )
        self.assertIsNotNone(order)
        # not confirmed
        sales = db.product_sales_counts(self.shop)
        self.assertNotIn(self.pids[1], sales)


if __name__ == "__main__":
    unittest.main()
