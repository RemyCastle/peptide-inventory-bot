"""Kit price must be settable when a product is CREATED, not only edited."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import inventory_import  # noqa: E402
import webpanel  # noqa: E402

SHOP = 4100


class KitTokenParseTests(unittest.TestCase):
    def test_kit_token_anywhere_after_stock(self):
        for line, kit, unit, desc in (
            ("BPC | 41 | 10 | kit:294", 294.0, "vial", ""),
            ("BPC | 41 | 10 | kit=294", 294.0, "vial", ""),
            ("BPC | 41 | 10 | kit: $294.50", 294.5, "vial", ""),
            ("BPC | 41 | 10 | vial | kit:294 | great", 294.0, "vial", "great"),
            ("BPC | 41 | 10 | kit:1,294", 1294.0, "vial", ""),
        ):
            res = inventory_import.parse_inventory_text(line)
            self.assertEqual(res.errors, [], line)
            row = res.rows[0]
            self.assertEqual(row.kit_price, kit, line)
            self.assertEqual(row.unit, unit, line)
            self.assertEqual(row.description, desc, line)

    def test_legacy_lines_unaffected(self):
        res = inventory_import.parse_inventory_text(
            "Tren Ace | 45.00 | 10 | vial | acetate blend\n"
            "Test E | 30 | 5 | bottle |\n"
            "Old | 20 | 2 | just a description"
        )
        self.assertEqual(res.errors, [])
        self.assertIsNone(res.rows[0].kit_price)
        self.assertEqual(res.rows[0].unit, "vial")
        self.assertEqual(res.rows[0].description, "acetate blend")
        self.assertEqual(res.rows[2].description, "just a description")

    def test_kit_word_in_description_is_not_a_price(self):
        res = inventory_import.parse_inventory_text(
            "BPC | 41 | 10 | vial | comes in a kit box"
        )
        self.assertIsNone(res.rows[0].kit_price)
        self.assertEqual(res.rows[0].description, "comes in a kit box")


class KitCreateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "kit.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Kit Shop")
        webpanel.ensure_webpanel_tables()
        self.tok = {"chat_id": SHOP, "user_id": 1}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_import_sets_kit_price_on_create_and_update(self):
        inventory_import.import_from_text(
            SHOP, "BPC-157 10MG | 41 | 10 | kit:294", mode="upsert"
        )
        p = {x["name"]: x for x in db.list_products(SHOP, active_only=False)}
        self.assertEqual(p["BPC-157 10MG"]["kit_price"], 294.0)
        # updating with a new kit price changes it
        inventory_import.import_from_text(
            SHOP, "BPC-157 10MG | 41 | 10 | kit:310", mode="upsert"
        )
        p = {x["name"]: x for x in db.list_products(SHOP, active_only=False)}
        self.assertEqual(p["BPC-157 10MG"]["kit_price"], 310.0)

    def test_import_without_kit_leaves_existing_kit_alone(self):
        inventory_import.import_from_text(
            SHOP, "BPC | 41 | 10 | kit:294", mode="upsert"
        )
        inventory_import.import_from_text(SHOP, "BPC | 45 | 12", mode="upsert")
        p = {x["name"]: x for x in db.list_products(SHOP, active_only=False)}
        self.assertEqual(p["BPC"]["price"], 45.0)
        self.assertEqual(p["BPC"]["kit_price"], 294.0)

    def test_panel_new_product_accepts_kit_price(self):
        code, data = webpanel.api_product(
            self.tok, {"name": "SEMA 10MG", "price": 60, "stock": 5, "kit_price": 480}
        )
        self.assertEqual(code, 200, data)
        self.assertEqual(db.get_product(data["id"])["kit_price"], 480.0)

    def test_panel_new_product_without_kit_is_vial_only(self):
        code, data = webpanel.api_product(
            self.tok, {"name": "KPV", "price": 36, "stock": 5, "kit_price": None}
        )
        self.assertEqual(code, 200, data)
        self.assertIsNone(db.get_product(data["id"])["kit_price"])

    def test_kit_price_reaches_the_buyer_offer(self):
        code, data = webpanel.api_product(
            self.tok, {"name": "SEMA", "price": 60, "stock": 50, "kit_price": 480}
        )
        p = db.get_product(data["id"])
        self.assertTrue(db.kit_option_available(p, stock=50))
        self.assertEqual(db.product_kit_price(p), 480.0)


if __name__ == "__main__":
    unittest.main()
