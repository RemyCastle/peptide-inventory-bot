"""Tests: stock only drops on admin confirm; audit log records deductions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class StockConfirmAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.db"
        db.set_db_path(self.db_path)
        db.init_db()
        self.shop_id = 1001
        self.admin_id = 42
        self.buyer_id = 99
        db.ensure_shop(self.shop_id, title="Test Shop")
        db.update_shop(self.shop_id, low_stock_threshold=2)
        self.pid = db.add_product(
            self.shop_id, name="Test Vial", price=50.0, stock=5, description="t"
        )
        db.add_payment_method(self.shop_id, "Cash", "Pay cash")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _create_order(self, qty: int = 3) -> dict:
        pm = db.list_payment_methods(self.shop_id)[0]
        order = db.create_order(
            chat_id=self.shop_id,
            user_id=self.buyer_id,
            username="buyer",
            full_name="Buyer",
            items=[{"product_id": self.pid, "quantity": qty}],
            payment_method=pm,
            ship_name="Buyer",
            ship_address="123 Test St",
        )
        assert order is not None
        return order

    def test_create_order_does_not_deduct_stock(self) -> None:
        self._create_order(qty=2)
        prod = db.get_product(self.pid)
        self.assertEqual(int(prod["stock"]), 5)
        self.assertEqual(db.list_stock_audit(chat_id=self.shop_id), [])

    def test_confirm_deducts_stock_and_writes_audit(self) -> None:
        order = self._create_order(qty=3)
        ok, msg, alerts = db.confirm_order_payment(order["id"], self.admin_id)
        self.assertTrue(ok, msg)
        prod = db.get_product(self.pid)
        self.assertEqual(int(prod["stock"]), 2)

        audit = db.list_stock_audit(product_id=self.pid)
        self.assertEqual(len(audit), 1)
        row = audit[0]
        self.assertEqual(int(row["delta"]), -3)
        self.assertEqual(int(row["stock_before"]), 5)
        self.assertEqual(int(row["stock_after"]), 2)
        self.assertEqual(row["reason"], "order_paid_confirm")
        self.assertEqual(int(row["actor_id"]), self.admin_id)
        self.assertEqual(int(row["order_id"]), order["id"])

        paid = db.get_order(order["id"])
        self.assertEqual(paid["status"], "paid")

    def test_confirm_idempotent_no_double_deduct(self) -> None:
        order = self._create_order(qty=1)
        ok1, _, _ = db.confirm_order_payment(order["id"], self.admin_id)
        ok2, msg2, _ = db.confirm_order_payment(order["id"], self.admin_id)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("already", msg2.lower())
        prod = db.get_product(self.pid)
        self.assertEqual(int(prod["stock"]), 4)
        self.assertEqual(len(db.list_stock_audit(product_id=self.pid)), 1)

    def test_low_stock_alert_when_at_or_below_threshold(self) -> None:
        order = self._create_order(qty=3)  # 5 -> 2, threshold 2
        ok, _, alerts = db.confirm_order_payment(order["id"], self.admin_id)
        self.assertTrue(ok)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["name"], "Test Vial")
        self.assertEqual(alerts[0]["stock"], 2)
        self.assertEqual(alerts[0]["threshold"], 2)

    def test_reject_does_not_deduct_or_audit(self) -> None:
        order = self._create_order(qty=2)
        ok, msg = db.reject_order(order["id"], self.admin_id, "nope")
        self.assertTrue(ok, msg)
        prod = db.get_product(self.pid)
        self.assertEqual(int(prod["stock"]), 5)
        self.assertEqual(db.list_stock_audit(chat_id=self.shop_id), [])

    def test_manual_adjust_writes_audit(self) -> None:
        new = db.adjust_stock(self.pid, -1, actor_id=self.admin_id, reason="manual_adjust")
        self.assertEqual(new, 4)
        audit = db.list_stock_audit(product_id=self.pid)
        self.assertEqual(len(audit), 1)
        self.assertEqual(int(audit[0]["delta"]), -1)

    def test_schema_version(self) -> None:
        self.assertGreaterEqual(db.get_schema_version(), 2)

    def test_shop_display_falls_back_to_instance_defaults(self) -> None:
        shop = db.get_shop(self.shop_id)
        display = db.shop_display(shop)
        self.assertIn("brand_name", display)
        self.assertIn("currency_symbol", display)
        self.assertEqual(int(display["low_stock_threshold"]), 2)


if __name__ == "__main__":
    unittest.main()
