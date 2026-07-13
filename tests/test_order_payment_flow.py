"""Tests for payment codes, proof, tracking, and confirm."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class OrderPaymentFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._td.name) / "o.db")
        db.init_db()
        self.shop = 7
        db.ensure_shop(self.shop, title="S")
        self.pid = db.add_product(self.shop, "Reta 10mg", 99.0, stock=5)
        self.pay = {"id": None, "name": "Cash App"}

    def tearDown(self) -> None:
        self._td.cleanup()

    def _order(self, qty: int = 1) -> dict:
        o = db.create_order(
            self.shop,
            1001,
            "buyer",
            "Buyer Name",
            [
                {
                    "product_id": self.pid,
                    "product_name": "Reta 10mg",
                    "unit_price": 99.0,
                    "quantity": qty,
                }
            ],
            self.pay,
            "Buyer Name",
            "1 Main St, Springfield OR",
            "Leave at door",
        )
        self.assertIsNotNone(o)
        return o

    def test_payment_code_assigned(self) -> None:
        o = self._order()
        self.assertTrue(o.get("payment_code"))
        self.assertTrue(str(o["payment_code"]).startswith("UF"))
        self.assertIn(str(o["id"]), o["payment_code"])

    def test_codes_unique(self) -> None:
        codes = {self._order()["payment_code"] for _ in range(5)}
        self.assertEqual(len(codes), 5)

    def test_proof_and_confirm_with_tracking(self) -> None:
        o = self._order()
        self.assertTrue(
            db.mark_order_awaiting_confirmation(
                o["id"], proof_file_id="AgADPROOF", proof_file_type="photo"
            )
        )
        o = db.get_order(o["id"])
        self.assertEqual(o["status"], "awaiting_confirmation")
        self.assertEqual(o["payment_proof_file_id"], "AgADPROOF")
        ok, msg, _ = db.confirm_order_payment(
            o["id"], 42, tracking_number="9400TEST", tracking_carrier="USPS"
        )
        self.assertTrue(ok, msg)
        o = db.get_order(o["id"])
        self.assertEqual(o["status"], "paid")
        self.assertEqual(o["tracking_number"], "9400TEST")
        self.assertEqual(o["tracking_carrier"], "USPS")
        self.assertEqual(db.get_product(self.pid)["stock"], 4)
        summary = db.format_order_summary(o, db.get_order_items(o["id"]))
        self.assertIn("9400TEST", summary)
        self.assertIn(o["payment_code"], summary)

    def test_confirm_without_tracking_then_add(self) -> None:
        o = self._order()
        db.mark_order_awaiting_confirmation(o["id"])
        ok, _, _ = db.confirm_order_payment(o["id"], 42, tracking_number="-")
        self.assertTrue(ok)
        o = db.get_order(o["id"])
        self.assertFalse((o.get("tracking_number") or "").strip())
        self.assertTrue(db.set_order_tracking(o["id"], "TRACK123", "UPS"))
        o = db.get_order(o["id"])
        self.assertEqual(o["tracking_number"], "TRACK123")


if __name__ == "__main__":
    unittest.main()
