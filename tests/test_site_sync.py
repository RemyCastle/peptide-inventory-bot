"""Website catalog sync: create / update / deactivate / adopt-by-name / keep stock."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import site_sync  # noqa: E402

SHOP = 555


def site_product(pid, name, vial, pack, kit_only=False, sort=0, active=True):
    return {
        "id": pid,
        "name": name,
        "vial_price": vial,
        "pack_price": pack,
        "kit_only": kit_only,
        "sort_order": sort,
        "active": active,
    }


FEED_V1 = [
    site_product(1, "SEMA 10MG", 60, 480, sort=1),
    site_product(2, "RETA 35 MG", 168, 1344, sort=2),
    site_product(3, "STARTER KIT", None, 120, kit_only=True, sort=3),
]


class SiteSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "sync.db")
        db.init_db()
        db.ensure_shop(SHOP, title="SPBC Shop")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sync(self, feed):
        with mock.patch.object(site_sync, "fetch_site_products", lambda base_url=None: feed):
            return site_sync.sync_shop(chat_id=SHOP)

    def _by_name(self):
        return {p["name"]: p for p in db.list_products(SHOP, active_only=False)}

    def test_first_sync_creates_all_with_zero_stock(self):
        result = self._sync(FEED_V1)
        self.assertEqual(len(result.created), 3)
        prods = self._by_name()
        sema = prods["SEMA 10MG"]
        self.assertEqual(sema["price"], 60.0)
        self.assertEqual(sema["kit_price"], 480.0)
        self.assertEqual(sema["unit"], "vial")
        self.assertEqual(sema["stock"], 0)
        self.assertEqual(str(sema["site_key"]), "1")
        kit = prods["STARTER KIT"]
        self.assertEqual(kit["price"], 120.0)
        self.assertEqual(kit["unit"], "kit")
        self.assertIsNone(kit["kit_price"])

    def test_second_sync_is_unchanged(self):
        self._sync(FEED_V1)
        result = self._sync(FEED_V1)
        self.assertFalse(result.changed)
        self.assertEqual(result.unchanged, 3)

    def test_price_change_updates_but_keeps_stock(self):
        self._sync(FEED_V1)
        pid = self._by_name()["SEMA 10MG"]["id"]
        db.update_product(pid, stock=42)
        feed = [dict(FEED_V1[0], vial_price=65), FEED_V1[1], FEED_V1[2]]
        result = self._sync(feed)
        self.assertEqual(result.updated, ["SEMA 10MG"])
        sema = self._by_name()["SEMA 10MG"]
        self.assertEqual(sema["price"], 65.0)
        self.assertEqual(sema["stock"], 42)

    def test_removed_site_product_deactivates(self):
        self._sync(FEED_V1)
        result = self._sync(FEED_V1[:2])
        self.assertEqual(result.deactivated, ["STARTER KIT"])
        self.assertEqual(self._by_name()["STARTER KIT"]["active"], 0)

    def test_manual_products_untouched(self):
        db.add_product(SHOP, "HOUSE BLEND", 25.0, 9)
        self._sync(FEED_V1)
        result = self._sync(FEED_V1[:1])
        blend = self._by_name()["HOUSE BLEND"]
        self.assertEqual(blend["active"], 1)
        self.assertEqual(blend["stock"], 9)
        self.assertNotIn("HOUSE BLEND", result.deactivated)

    def test_existing_product_adopted_by_name(self):
        pid = db.add_product(SHOP, "sema 10mg", 55.0, 7)
        result = self._sync(FEED_V1[:1])
        self.assertEqual(result.updated, ["SEMA 10MG"])
        self.assertEqual(result.created, [])
        prods = self._by_name()
        self.assertIn("SEMA 10MG", prods)  # renamed to site casing
        adopted = prods["SEMA 10MG"]
        self.assertEqual(adopted["id"], pid)
        self.assertEqual(str(adopted["site_key"]), "1")
        self.assertEqual(adopted["stock"], 7)
        self.assertEqual(adopted["price"], 60.0)

    def test_rename_on_site_follows_site_key(self):
        self._sync(FEED_V1)
        feed = [dict(FEED_V1[0], name="SEMAGLUTIDE 10MG"), FEED_V1[1], FEED_V1[2]]
        result = self._sync(feed)
        self.assertEqual(result.updated, ["SEMAGLUTIDE 10MG"])
        prods = self._by_name()
        self.assertIn("SEMAGLUTIDE 10MG", prods)
        self.assertNotIn("SEMA 10MG", prods)


if __name__ == "__main__":
    unittest.main()
