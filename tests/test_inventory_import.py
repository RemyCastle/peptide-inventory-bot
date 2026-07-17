"""Inventory layout text import (parse + add-only create)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import inventory_import as inv  # noqa: E402


class ParseInventoryTests(unittest.TestCase):
    def test_happy_path_and_comments(self) -> None:
        text = """
# comment
Tren Ace | 45.00 | 10 | acetate blend
Test E | 30 | 5

HCG 5000 | 55 | 0 | fridge
"""
        p = inv.parse_inventory_text(text)
        self.assertEqual(len(p.errors), 0)
        self.assertEqual(len(p.rows), 3)
        self.assertEqual(p.rows[0].name, "Tren Ace")
        self.assertEqual(p.rows[0].price, 45.0)
        self.assertEqual(p.rows[0].stock, 10)
        self.assertEqual(p.rows[0].description, "acetate blend")
        self.assertEqual(p.rows[1].description, "")

    def test_header_skipped(self) -> None:
        text = "name | price | stock\nAlpha | 10 | 1\n"
        p = inv.parse_inventory_text(text)
        self.assertEqual(len(p.rows), 1)
        self.assertEqual(p.rows[0].name, "Alpha")

    def test_bad_price_and_stock(self) -> None:
        text = "A | abc | 1\nB | 10 | -2\nC | 0 | 1\nD | 5 | x\n"
        p = inv.parse_inventory_text(text)
        self.assertEqual(len(p.rows), 0)
        self.assertGreaterEqual(len(p.errors), 3)

    def test_dollar_price(self) -> None:
        p = inv.parse_inventory_text("Item | $12.50 | 3\n")
        self.assertEqual(len(p.rows), 1)
        self.assertEqual(p.rows[0].price, 12.5)

    def test_template_parses_examples(self) -> None:
        p = inv.parse_inventory_text(inv.TEMPLATE_TEXT)
        self.assertGreaterEqual(len(p.rows), 2)
        self.assertEqual(len(p.errors), 0)

    def test_empty_file(self) -> None:
        p = inv.parse_inventory_text("# only comments\n\n")
        self.assertEqual(len(p.rows), 0)
        self.assertTrue(p.errors)


class ImportProductsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "imp.db")
        db.init_db()
        self.shop_a = 11
        self.shop_b = 22
        db.ensure_shop(self.shop_a, title="A")
        db.ensure_shop(self.shop_b, title="B")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_products(self) -> None:
        text = "Tren Ace | 45 | 10\nTest E | 30 | 5 | desc\n"
        parsed, imported = inv.import_from_text(self.shop_a, text)
        self.assertEqual(len(parsed.errors), 0)
        self.assertEqual(imported.created_count, 2)
        prods = db.list_products(self.shop_a)
        self.assertEqual(len(prods), 2)
        names = {p["name"] for p in prods}
        self.assertEqual(names, {"Tren Ace", "Test E"})

    def test_skip_existing_by_name(self) -> None:
        db.add_product(self.shop_a, "Tren Ace", 40.0, 1)
        text = "tren ace | 99 | 50\nNew One | 10 | 2\n"
        _, imported = inv.import_from_text(self.shop_a, text)
        self.assertEqual(imported.created_count, 1)
        self.assertEqual(imported.skipped_count, 1)
        self.assertIn("tren ace", [s.casefold() for s in imported.skipped])
        # existing price/stock unchanged
        tren = [p for p in db.list_products(self.shop_a) if p["name"] == "Tren Ace"][0]
        self.assertEqual(float(tren["price"]), 40.0)
        self.assertEqual(int(tren["stock"]), 1)

    def test_duplicate_lines_in_file(self) -> None:
        text = "Same | 10 | 1\nSame | 20 | 5\n"
        _, imported = inv.import_from_text(self.shop_a, text)
        self.assertEqual(imported.created_count, 1)
        self.assertEqual(imported.skipped_count, 1)
        self.assertEqual(len(db.list_products(self.shop_a)), 1)

    def test_shop_isolation(self) -> None:
        db.add_product(self.shop_a, "Shared Name", 1.0, 1)
        text = "Shared Name | 50 | 9\n"
        _, imported = inv.import_from_text(self.shop_b, text)
        self.assertEqual(imported.created_count, 1)
        self.assertEqual(len(db.list_products(self.shop_b)), 1)

    def test_decode_rejects_huge(self) -> None:
        with self.assertRaises(ValueError):
            inv.decode_upload_bytes(b"x" * (inv.MAX_FILE_BYTES + 1))

    def test_summary_mentions_counts(self) -> None:
        parsed, imported = inv.import_from_text(
            self.shop_a, "A | 1 | 1\nbad line\n"
        )
        s = inv.format_import_summary(parsed, imported)
        self.assertIn("Created", s)
        self.assertIn("1", s)


if __name__ == "__main__":
    unittest.main()
