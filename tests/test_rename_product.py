"""Tests for admin product rename (shop-scoped)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class RenameProductTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmpdir.name) / "rename.db")
        db.init_db()
        self.shop = 55
        self.other = 66
        db.ensure_shop(self.shop, title="A")
        db.ensure_shop(self.other, title="B")
        self.pid = db.add_product(self.shop, name="Old Name", price=10.0, stock=3)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_rename_ok(self) -> None:
        ok, name = db.rename_product(self.pid, self.shop, "  New Name  ")
        self.assertTrue(ok)
        self.assertEqual(name, "New Name")
        p = db.get_product(self.pid)
        self.assertEqual(p["name"], "New Name")
        self.assertEqual(int(p["stock"]), 3)
        self.assertEqual(float(p["price"]), 10.0)

    def test_empty_rejected(self) -> None:
        ok, msg = db.rename_product(self.pid, self.shop, "   ")
        self.assertFalse(ok)
        self.assertIn("empty", msg.lower())
        self.assertEqual(db.get_product(self.pid)["name"], "Old Name")

    def test_cross_shop_rejected(self) -> None:
        ok, msg = db.rename_product(self.pid, self.other, "Hacked")
        self.assertFalse(ok)
        self.assertEqual(db.get_product(self.pid)["name"], "Old Name")

    def test_too_long_rejected(self) -> None:
        ok, msg = db.rename_product(self.pid, self.shop, "x" * 200)
        self.assertFalse(ok)
        self.assertIn("long", msg.lower())


if __name__ == "__main__":
    unittest.main()
