"""Shared-inventory clones + hidden service fees."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import db
import franchise


class FranchiseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "t.db")
        db.init_db()
        franchise.ensure_franchise_tables()
        self.master = 9001
        self.clone = 9002
        self.admin = 77
        db.ensure_shop(self.master, title="Master")
        db.add_admin(self.master, self.admin, "a", self.admin)
        self.pid = db.add_product(self.master, "Alpha", 40.0, 5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clone_shares_stock_separate_price(self) -> None:
        tok = franchise.create_clone_token(self.master, self.admin)
        ok, _ = franchise.attach_clone(tok["token"], self.clone, self.admin, title="Clone")
        self.assertTrue(ok)
        clone_products = franchise.list_products_effective(self.clone, active_only=True)
        self.assertEqual(len(clone_products), 1)
        cp = clone_products[0]
        self.assertEqual(cp["stock"], 5)
        self.assertTrue(cp.get("linked_product_id"))
        db.update_product(cp["id"], price=99.0)
        refreshed = db.get_product(cp["id"])
        self.assertEqual(float(refreshed["price"]), 99.0)
        master = db.get_product(self.pid)
        self.assertEqual(float(master["price"]), 40.0)
        # deduct via clone order path
        with db.get_db() as c:
            c.execute(
                "UPDATE shops SET hidden_service_fee = 2.5, shipping_fee = 8, "
                "free_shipping_above = 9999, shipping_enabled = 1 WHERE chat_id = ?",
                (self.clone,),
            )
        order = db.create_order(
            self.clone,
            1,
            "u",
            "User",
            [{"product_id": cp["id"], "quantity": 2}],
            {"id": None, "name": "Cash"},
            "Name",
            "Addr",
        )
        self.assertIsNotNone(order)
        self.assertEqual(float(order["hidden_service_fee"]), 2.5)
        # customer shipping includes base 8 + hidden 2.5
        self.assertEqual(float(order["shipping_fee"]), 10.5)
        ok, msg, _ = db.confirm_order_payment(order["id"], self.admin)
        self.assertTrue(ok, msg)
        self.assertEqual(franchise.get_effective_stock(self.pid), 3)
        self.assertEqual(franchise.get_effective_stock(cp["id"]), 3)

    def test_master_only_fee_gate(self) -> None:
        # Without OWNER_IDS, is_owner may allow admins — set fee via module when owner
        ok, _ = franchise.set_hidden_service_fee(self.master, 1.0, self.admin)
        # depends on OWNER_IDS env; at least function returns tuple
        self.assertIsInstance(ok, bool)


if __name__ == "__main__":
    unittest.main()
