"""UX batch helpers: status labels, saved address, shipped flow, reorder cart."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402

SHOP = 3000
BUYER = 77


def _make_order(**kw):
    method = {"id": None, "name": "Cash App", "instructions": "pay $x"}
    items = [
        {
            "product_id": kw.pop("product_id"),
            "product_name": kw.pop("product_name"),
            "unit_price": kw.pop("unit_price", 10.0),
            "quantity": kw.pop("quantity", 1),
        }
    ]
    return db.create_order(
        chat_id=SHOP,
        user_id=BUYER,
        username="buyer",
        full_name="Buyer B",
        items=items,
        payment_method=method,
        ship_name=kw.pop("ship_name", "Buyer B"),
        ship_address=kw.pop("ship_address", "1 Main St, Springfield"),
        ship_notes="",
    )


class UxHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "ux.db")
        db.init_db()
        db.ensure_shop(SHOP, title="UX Shop")
        self.pid = db.add_product(SHOP, "BPC", 10.0, 50)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_status_labels_human(self):
        self.assertIn("review", db.status_label("awaiting_confirmation").lower())
        self.assertIn("Shipped", db.status_label("shipped"))
        self.assertEqual(db.status_label("weird_status"), "weird_status")

    def test_last_ship_details_returns_most_recent(self):
        self.assertIsNone(db.last_ship_details(BUYER))
        _make_order(product_id=self.pid, product_name="BPC",
                    ship_name="Old Name", ship_address="Old Addr")
        _make_order(product_id=self.pid, product_name="BPC",
                    ship_name="New Name", ship_address="New Addr")
        saved = db.last_ship_details(BUYER)
        self.assertEqual(saved["ship_name"], "New Name")
        self.assertEqual(saved["ship_address"], "New Addr")

    def test_mark_shipped_requires_paid(self):
        order = _make_order(product_id=self.pid, product_name="BPC")
        oid = int(order["id"])
        ok, msg = db.mark_order_shipped(oid)
        self.assertFalse(ok)
        db.mark_order_awaiting_confirmation(oid)
        db.confirm_order_payment(oid, admin_id=1)
        ok, msg = db.mark_order_shipped(oid)
        self.assertTrue(ok, msg)
        self.assertEqual(db.get_order(oid)["status"], "shipped")
        ok2, msg2 = db.mark_order_shipped(oid)
        self.assertFalse(ok2)
        self.assertIn("Already", msg2)

    def test_cart_entries_from_items_rebuilds_kits(self):
        items = [
            {"product_id": 5, "product_name": "SEMA 10MG (kit of 10)", "quantity": 20},
            {"product_id": 5, "product_name": "SEMA 10MG", "quantity": 3},
            {"product_id": 9, "product_name": "BPC", "quantity": 2},
            {"product_id": None, "product_name": "ghost", "quantity": 1},
        ]
        entries = db.cart_entries_from_items(items, 10)
        self.assertEqual(entries[5], {"singles": 3, "kits": 2})
        self.assertEqual(entries[9], {"singles": 2, "kits": 0})
        self.assertNotIn(None, entries)


if __name__ == "__main__":
    unittest.main()
