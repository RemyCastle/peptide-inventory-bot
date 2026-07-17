"""Unit tests for per-shop product search."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


class SearchProductsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmpdir.name) / "search.db")
        db.init_db()
        self.shop_a = 101
        self.shop_b = 202
        db.ensure_shop(self.shop_a, title="Shop A")
        db.ensure_shop(self.shop_b, title="Shop B")
        self.tren = db.add_product(
            self.shop_a, name="Tren Ace", price=40.0, stock=5, description="acetate blend"
        )
        self.test = db.add_product(
            self.shop_a, name="Test E", price=30.0, stock=0, description="enanthate"
        )
        self.other = db.add_product(
            self.shop_b, name="Tren Ace", price=99.0, stock=9, description="other shop"
        )
        db.add_product(
            self.shop_a, name="Hidden Inactive", price=1.0, stock=3, description="x"
        )
        # deactivate last
        prods = db.list_products(self.shop_a, active_only=False)
        hid = [p for p in prods if p["name"] == "Hidden Inactive"][0]
        db.update_product(hid["id"], active=0)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_exact_and_partial_match(self) -> None:
        hits = db.search_products(self.shop_a, "Tren")
        names = [h["name"] for h in hits]
        self.assertIn("Tren Ace", names)
        self.assertNotIn("Test E", names)

    def test_description_match(self) -> None:
        hits = db.search_products(self.shop_a, "enanthate")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["name"], "Test E")

    def test_case_insensitive(self) -> None:
        hits = db.search_products(self.shop_a, "tReN")
        self.assertTrue(any(h["name"] == "Tren Ace" for h in hits))

    def test_shop_scoping_no_leakage(self) -> None:
        hits_a = db.search_products(self.shop_a, "Tren")
        hits_b = db.search_products(self.shop_b, "Tren")
        self.assertTrue(all(h["chat_id"] == self.shop_a for h in hits_a))
        self.assertTrue(all(h["chat_id"] == self.shop_b for h in hits_b))
        self.assertEqual(hits_b[0]["price"], 99.0)
        self.assertNotEqual(hits_a[0]["price"], hits_b[0]["price"])

    def test_mirrors_catalog_includes_out_of_stock_active(self) -> None:
        """Browse shows active out-of-stock; search should too."""
        hits = db.search_products(self.shop_a, "Test")
        self.assertEqual(len(hits), 1)
        self.assertEqual(int(hits[0]["stock"]), 0)

    def test_inactive_excluded(self) -> None:
        hits = db.search_products(self.shop_a, "Hidden")
        self.assertEqual(hits, [])

    def test_empty_query(self) -> None:
        self.assertEqual(db.search_products(self.shop_a, "  "), [])


if __name__ == "__main__":
    unittest.main()
