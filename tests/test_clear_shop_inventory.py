"""Owner-only clear shop inventory."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class ClearShopInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "clear.db")
        db.init_db()
        self.shop = 7001
        self.other = 7002
        self.owner_id = 111
        self.admin_id = 222
        db.ensure_shop(self.shop, title="ClearMe")
        db.ensure_shop(self.other, title="Other")
        db.add_admin(self.shop, self.admin_id, "adm", self.admin_id)
        db.add_product(self.shop, "A", 10.0, 5)
        db.add_product(self.shop, "B", 20.0, 3)
        db.add_product(self.other, "Keep", 1.0, 9)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_owner_clears_only_this_shop(self) -> None:
        with mock.patch.object(db, "OWNER_IDS", {self.owner_id}):
            # reload is_owner uses OWNER_IDS from module
            ok, msg, n = db.clear_shop_inventory(self.shop, self.owner_id)
        self.assertTrue(ok, msg)
        self.assertEqual(n, 2)
        self.assertEqual(len(db.list_products(self.shop, active_only=False)), 0)
        self.assertEqual(len(db.list_products(self.other, active_only=False)), 1)

    def test_shop_admin_denied(self) -> None:
        with mock.patch.object(db, "OWNER_IDS", {self.owner_id}):
            ok, msg, n = db.clear_shop_inventory(self.shop, self.admin_id)
        self.assertFalse(ok)
        self.assertEqual(n, 0)
        self.assertEqual(len(db.list_products(self.shop, active_only=False)), 2)
        self.assertIn("owner", msg.lower())

    def test_empty_shop(self) -> None:
        empty = 7003
        db.ensure_shop(empty, title="Empty")
        with mock.patch.object(db, "OWNER_IDS", {self.owner_id}):
            ok, msg, n = db.clear_shop_inventory(empty, self.owner_id)
        self.assertTrue(ok)
        self.assertEqual(n, 0)

    def test_writes_stock_audit(self) -> None:
        with mock.patch.object(db, "OWNER_IDS", {self.owner_id}):
            db.clear_shop_inventory(self.shop, self.owner_id)
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT reason FROM stock_audit WHERE chat_id = ?",
                (self.shop,),
            ).fetchall()
        reasons = [r["reason"] for r in rows]
        self.assertTrue(all(r == "owner_clear_inventory" for r in reasons))
        self.assertEqual(len(reasons), 2)


if __name__ == "__main__":
    unittest.main()
