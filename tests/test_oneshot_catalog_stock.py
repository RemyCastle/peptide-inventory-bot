"""ONE-SHOT catalog stock=100 is shop-scoped and never wipes inventory.db."""

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
import oneshot_catalog_stock as oneshot  # noqa: E402
import run_cloud  # noqa: E402
import spbc_notify  # noqa: E402
import unicorn_shop  # noqa: E402

CATALOG = oneshot.EXPECTED_CHAT_ID
OTHER = -1001
STRAY_UNICORN = -1002


class OneshotCatalogStockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        db.set_db_path(self.root / "live.db")
        db.init_db()
        db.ensure_shop(CATALOG, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER, title="Helix Bio Labs")
        self.p10 = db.add_product(CATALOG, "BPC-157 5MG", 40.0, stock=10)
        self.p0 = db.add_product(CATALOG, "TB-500 10MG", 47.0, stock=0)
        self.p7 = db.add_product(CATALOG, "CJC-1295 5MG", 55.0, stock=7)
        self.inactive = db.add_product(CATALOG, "Old Lot", 1.0, stock=10)
        with db.get_db() as conn:
            conn.execute("UPDATE products SET active = 0 WHERE id = ?", (self.inactive,))
        self.other = db.add_product(OTHER, "Keep Out", 9.0, stock=99)
        self.other2 = db.add_product(OTHER, "Also Keep", 3.0, stock=5)
        os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
        spbc_notify._oneshot_stock_last = None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sets_all_catalog_rows_leaves_other_shop(self) -> None:
        path = Path(db.get_db_path())
        size_before = path.stat().st_size
        result = oneshot.apply_oneshot_catalog_stock()
        self.assertTrue(result["ok"], result)
        self.assertEqual(int(result["sku_before"]), 4)
        self.assertEqual(int(result["sku_after"]), 4)
        self.assertEqual(int(result["n100_before"]), 0)
        self.assertEqual(int(result["n100_after"]), 4)
        self.assertEqual(int(result["n0_before"]), 1)
        self.assertEqual(int(result["n0_after"]), 0)
        self.assertEqual(int(result["applied"]), 4)
        self.assertTrue(result["other_shops_unchanged"])
        self.assertEqual(int(result["other_shop_count"]), 1)
        for pid in (self.p10, self.p0, self.p7, self.inactive):
            self.assertEqual(int(db.get_product(pid)["stock"]), 100)
        self.assertEqual(int(db.get_product(self.other)["stock"]), 99)
        self.assertEqual(int(db.get_product(self.other2)["stock"]), 5)
        self.assertEqual(len(db.list_products(CATALOG, active_only=False)), 4)
        self.assertEqual(len(db.list_products(OTHER, active_only=False)), 2)
        self.assertTrue(path.is_file())
        self.assertGreaterEqual(path.stat().st_size, size_before)
        self.assertTrue(oneshot.marker_exists())

    def test_second_run_is_noop(self) -> None:
        oneshot.apply_oneshot_catalog_stock()
        db.adjust_stock(self.p10, -3, reason="sale")
        result = oneshot.apply_oneshot_catalog_stock()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["skipped"], "already_applied")
        self.assertEqual(int(result["applied"]), 0)
        self.assertEqual(int(db.get_product(self.p10)["stock"]), 97)

    def test_aborts_when_catalog_shop_is_not_expected(self) -> None:
        db.ensure_shop(STRAY_UNICORN, title="Unicorn Magic Factory Spare")
        db.add_product(STRAY_UNICORN, "Spare Vial", 10.0, stock=12)
        with db.get_db() as conn:
            conn.execute("DELETE FROM products WHERE chat_id = ?", (CATALOG,))
        result = oneshot.apply_oneshot_catalog_stock()
        self.assertFalse(result["ok"], result)
        self.assertIn("expected Unicorn shop", str(result.get("skipped")))
        self.assertEqual(int(db.get_product(self.other)["stock"]), 99)
        spare = next(
            p
            for p in db.list_products(STRAY_UNICORN, active_only=False)
            if p["name"] == "Spare Vial"
        )
        self.assertEqual(int(spare["stock"]), 12)
        self.assertFalse(oneshot.marker_exists())

    def test_recall_skips_after_marker(self) -> None:
        oneshot.apply_oneshot_catalog_stock()
        db.ensure_shop(STRAY_UNICORN, title="Unicorn Magic Factory Spare")
        db.add_product(STRAY_UNICORN, "BPC-157 5MG", 40.0, stock=47)
        result = unicorn_shop.recall_catalog_stock()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "oneshot")
        self.assertEqual(result["skipped"], "oneshot_stock_100")
        self.assertEqual(int(db.get_product(self.p10)["stock"]), 100)

    def test_boot_skips_without_live_disk(self) -> None:
        with mock.patch.dict(os.environ, {"DB_PATH": str(db.get_db_path())}, clear=False):
            os.environ.pop("ONESHOT_CATALOG_STOCK", None)
            run_cloud._oneshot_catalog_stock()
        self.assertEqual(int(db.get_product(self.p10)["stock"]), 10)
        self.assertIsNone(spbc_notify._oneshot_stock_last)

    def test_boot_forced_records_health(self) -> None:
        env = {
            "ONESHOT_CATALOG_STOCK": "1",
            "DB_PATH": str(db.get_db_path()),
            "PUBLIC_BOT_USERNAME": "UnicornMagicFactory2Bot",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            run_cloud._oneshot_catalog_stock()
        self.assertEqual(int(db.get_product(self.p10)["stock"]), 100)
        self.assertEqual(int(db.get_product(self.other)["stock"]), 99)
        health = spbc_notify._status_body()
        self.assertEqual(health["oneshot_stock"]["ok"], True)
        self.assertEqual(health["oneshot_stock"]["sku_before"], 4)
        self.assertEqual(health["oneshot_stock"]["n100_after"], 4)
        self.assertTrue(health["oneshot_stock"]["other_shops_unchanged"])
        self.assertNotIn("chat_id", health["oneshot_stock"])

    def test_never_deletes_or_replaces_db(self) -> None:
        path = Path(db.get_db_path())
        ids_before = sorted(p["id"] for p in db.list_products(CATALOG, active_only=False))
        oneshot.apply_oneshot_catalog_stock()
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)
        ids_after = sorted(p["id"] for p in db.list_products(CATALOG, active_only=False))
        self.assertEqual(ids_before, ids_after)
        self.assertEqual(len(db.list_products(OTHER, active_only=False)), 2)


if __name__ == "__main__":
    unittest.main()
