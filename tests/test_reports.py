"""Unit tests for admin export report generators."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import reports  # noqa: E402


class ReportGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmpdir.name) / "reports.db")
        db.init_db()
        self.shop_a = 501
        self.shop_b = 502
        db.ensure_shop(self.shop_a, title="Alpha Shop")
        db.ensure_shop(self.shop_b, title="Beta Shop")
        db.update_shop(self.shop_a, low_stock_threshold=2)

        self.p1 = db.add_product(
            self.shop_a, name="Alpha Vial", price=40.0, stock=1, description="a"
        )
        self.p2 = db.add_product(
            self.shop_a, name="Plenty", price=10.0, stock=20, description="b"
        )
        db.add_product(self.shop_b, name="Other Shop Only", price=99.0, stock=5)

        pm = db.add_payment_method(self.shop_a, "Cash", "pay me")
        self.pm = db.get_payment_method(pm)

        # pending order
        self.order_pending = db.create_order(
            chat_id=self.shop_a,
            user_id=11,
            username="buyer1",
            full_name="Buyer One",
            items=[{"product_id": self.p2, "quantity": 1}],
            payment_method=self.pm,
            ship_name="Buyer One",
            ship_address="1 St",
        )
        # awaiting confirmation
        self.order_await = db.create_order(
            chat_id=self.shop_a,
            user_id=12,
            username="buyer2",
            full_name="Buyer Two",
            items=[{"product_id": self.p2, "quantity": 1}],
            payment_method=self.pm,
            ship_name="Buyer Two",
            ship_address="2 St",
        )
        db.mark_order_awaiting_confirmation(self.order_await["id"])

        # paid order — should NOT appear in pending report
        paid = db.create_order(
            chat_id=self.shop_a,
            user_id=13,
            username="paiduser",
            full_name="Paid User",
            items=[{"product_id": self.p2, "quantity": 1}],
            payment_method=self.pm,
            ship_name="Paid",
            ship_address="3 St",
        )
        db.confirm_order_payment(paid["id"], admin_id=1)

        # rejected
        rej = db.create_order(
            chat_id=self.shop_a,
            user_id=14,
            username="rej",
            full_name="Rej",
            items=[{"product_id": self.p2, "quantity": 1}],
            payment_method=self.pm,
            ship_name="R",
            ship_address="4 St",
        )
        db.reject_order(rej["id"], admin_id=1)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_inventory_includes_shop_products_only(self) -> None:
        text = reports.generate_inventory_report(self.shop_a)
        self.assertIn("Alpha Vial", text)
        self.assertIn("Plenty", text)
        self.assertIn("LOW", text)  # stock 1 under threshold 2
        self.assertNotIn("Other Shop Only", text)
        self.assertIn("Alpha Shop", text)

    def test_inventory_empty_state(self) -> None:
        empty_shop = 999
        db.ensure_shop(empty_shop, title="Empty")
        text = reports.generate_inventory_report(empty_shop)
        self.assertIn("No products in catalog", text)

    def test_pending_only_outstanding(self) -> None:
        text = reports.generate_pending_orders_report(self.shop_a)
        self.assertIn(f"Order #{self.order_pending['id']}", text)
        self.assertIn(f"Order #{self.order_await['id']}", text)
        self.assertIn("pending_payment", text)
        self.assertIn("awaiting_confirmation", text)
        self.assertNotIn("paiduser", text)
        self.assertNotIn("Paid User", text)
        self.assertNotIn("\n  Rej", text)  # rejected buyer not listed as open

    def test_pending_empty_state(self) -> None:
        empty_shop = 998
        db.ensure_shop(empty_shop, title="Empty2")
        text = reports.generate_pending_orders_report(empty_shop)
        self.assertIn("No pending orders", text)

    def test_full_report_combines(self) -> None:
        text = reports.generate_full_report(self.shop_a)
        self.assertIn("INVENTORY REPORT", text)
        self.assertIn("PENDING ORDERS REPORT", text)
        self.assertIn("Alpha Vial", text)

    def test_filename_safe(self) -> None:
        self.assertEqual(reports.safe_filename_part("My Shop!"), "My_Shop")
        self.assertTrue(len(reports.safe_filename_part("x" * 100)) <= 32)


if __name__ == "__main__":
    unittest.main()
