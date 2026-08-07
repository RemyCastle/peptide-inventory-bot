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
        self.assertEqual(str(sema["site_key"]), "S:1")
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
        self.assertEqual(str(adopted["site_key"]), "S:1")
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


GENERIC_FEED = [
    {"id": "a1", "name": "Tren Ace", "price": 45.0, "stock": 10, "unit": "vial"},
    {"sku": "b2", "name": "HCG 5000", "price": 55.0, "kit_price": 500.0},
]


class ShopSiteLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "links.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Franchise Shop")
        site_sync.ensure_site_links_table()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_link(self, feed, url="https://shop.example.com"):
        with mock.patch.object(site_sync, "_fetch_products_raw", lambda u: feed):
            ok, msg, link_id = site_sync.add_link(SHOP, url)
        self.assertTrue(ok, msg)
        return site_sync.get_link(link_id)

    def _sync(self, link, feed):
        with mock.patch.object(site_sync, "_fetch_products_raw", lambda u: feed):
            return site_sync.sync_link(link)

    def _by_name(self):
        return {p["name"]: p for p in db.list_products(SHOP, active_only=False)}

    def test_add_link_rejects_http(self):
        ok, msg, _ = site_sync.add_link(SHOP, "http://insecure.example.com")
        self.assertFalse(ok)

    def test_add_link_rejects_feed_without_products(self):
        with mock.patch.object(
            site_sync, "_fetch_products_raw", lambda u: [{"name": "x"}]
        ):
            ok, msg, _ = site_sync.add_link(SHOP, "https://bad.example.com")
        self.assertFalse(ok)

    def test_generic_feed_sync_follows_stock(self):
        link = self._add_link(GENERIC_FEED)
        result = self._sync(link, GENERIC_FEED)
        self.assertEqual(len(result.created), 2)
        prods = self._by_name()
        tren = prods["Tren Ace"]
        self.assertEqual(tren["stock"], 10)  # feed stock followed
        self.assertEqual(str(tren["site_key"]), f"L{link['id']}:a1")
        hcg = prods["HCG 5000"]
        self.assertEqual(hcg["stock"], 0)  # no stock in feed → bot-managed
        self.assertEqual(hcg["kit_price"], 500.0)
        # stock change on the site flows through
        feed2 = [dict(GENERIC_FEED[0], stock=3), GENERIC_FEED[1]]
        self._sync(link, feed2)
        self.assertEqual(self._by_name()["Tren Ace"]["stock"], 3)

    def test_link_and_env_namespaces_stay_disjoint(self):
        link = self._add_link(GENERIC_FEED)
        self._sync(link, GENERIC_FEED)
        with mock.patch.object(
            site_sync, "fetch_site_products", lambda base_url=None: FEED_V1
        ):
            site_sync.sync_shop(chat_id=SHOP)
        # env sync must NOT deactivate link products (different namespace)
        prods = self._by_name()
        self.assertEqual(prods["Tren Ace"]["active"], 1)
        self.assertEqual(prods["SEMA 10MG"]["active"], 1)
        # and link re-sync must not touch env products
        self._sync(link, GENERIC_FEED)
        self.assertEqual(self._by_name()["SEMA 10MG"]["active"], 1)

    def test_remove_link_deactivates_its_products(self):
        link = self._add_link(GENERIC_FEED)
        self._sync(link, GENERIC_FEED)
        self.assertTrue(site_sync.remove_link(int(link["id"]), SHOP))
        prods = self._by_name()
        self.assertEqual(prods["Tren Ace"]["active"], 0)
        self.assertEqual(prods["HCG 5000"]["active"], 0)
        self.assertEqual(site_sync.list_links(SHOP), [])

    def test_remove_link_wrong_shop_refused(self):
        link = self._add_link(GENERIC_FEED)
        self.assertFalse(site_sync.remove_link(int(link["id"]), SHOP + 1))

    def test_sync_all_links_covers_every_shop(self):
        other = SHOP + 100
        db.ensure_shop(other, title="Other Shop")
        link1 = self._add_link(GENERIC_FEED, "https://one.example.com")
        with mock.patch.object(site_sync, "_fetch_products_raw", lambda u: FEED_V1):
            ok, _, id2 = site_sync.add_link(other, "https://two.example.com")
        self.assertTrue(ok)
        with mock.patch.object(
            site_sync,
            "_fetch_products_raw",
            lambda u: GENERIC_FEED if "one" in u else FEED_V1,
        ):
            out = site_sync.sync_all_links()
        self.assertEqual(set(out), {int(link1["id"]), int(id2)})
        self.assertEqual(
            len(db.list_products(other, active_only=False)), len(FEED_V1)
        )


if __name__ == "__main__":
    unittest.main()
