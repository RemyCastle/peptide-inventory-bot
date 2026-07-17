"""Shops must not mix inventory unless collaboration (or franchise clone) is set up."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collab  # noqa: E402
import db  # noqa: E402


class ShopIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "iso.db")
        db.init_db()
        collab.ensure_collab_tables()
        self.a = 100
        self.b = 200
        self.c = 300
        self.admin = 1
        for cid, title in (
            (self.a, "ShopA"),
            (self.b, "ShopB"),
            (self.c, "ShopC"),
        ):
            db.ensure_shop(cid, title=title)
            db.add_admin(cid, self.admin, "a", self.admin)
        self.pa = db.add_product(self.a, "AlphaA", 10.0, 5)
        self.pb = db.add_product(self.b, "BetaB", 20.0, 7)
        self.pc = db.add_product(self.c, "GammaC", 30.0, 3)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_products_scoped(self) -> None:
        names_a = [p["name"] for p in db.list_products(self.a)]
        names_b = [p["name"] for p in db.list_products(self.b)]
        self.assertEqual(names_a, ["AlphaA"])
        self.assertEqual(names_b, ["BetaB"])
        self.assertNotIn(self.pa, [p["id"] for p in db.list_products(self.b)])

    def test_search_does_not_cross_shops(self) -> None:
        self.assertEqual(db.search_products(self.a, "Beta"), [])
        self.assertEqual(len(db.search_products(self.b, "Beta")), 1)
        self.assertEqual(db.search_products(self.c, "Alpha"), [])

    def test_catalog_own_only_without_collab(self) -> None:
        cat = collab.catalog_for_host(self.a)
        self.assertEqual([x["name"] for x in cat], ["AlphaA"])
        self.assertTrue(all(int(x["owner_chat_id"]) == self.a for x in cat))
        self.assertEqual(
            [x["name"] for x in collab.catalog_for_host(self.b)],
            ["BetaB"],
        )

    def test_collab_only_host_sees_guest_share(self) -> None:
        inv = collab.create_invite(self.a, self.admin, 10)
        ok, _ = collab.accept_invite(inv["token"], self.b, self.admin)
        self.assertTrue(ok)
        ok, _ = collab.set_share(self.a, self.b, self.pb, 15)
        self.assertTrue(ok)

        cat_a = collab.catalog_for_host(self.a)
        names = sorted(x["name"] for x in cat_a)
        self.assertEqual(names, ["AlphaA", "BetaB"])
        guest = [x for x in cat_a if x["name"] == "BetaB"][0]
        self.assertTrue(guest["is_guest"])
        self.assertEqual(int(guest["owner_chat_id"]), self.b)
        # sell price marked up
        self.assertAlmostEqual(float(guest["sell_price"]), 20.0 * 1.15, places=2)

        # Guest shop does not auto-list host products
        self.assertEqual(
            [x["name"] for x in collab.catalog_for_host(self.b)],
            ["BetaB"],
        )
        # Unrelated shop still isolated
        self.assertEqual(
            [x["name"] for x in collab.catalog_for_host(self.c)],
            ["GammaC"],
        )

    def test_order_rejects_unshared_foreign_product(self) -> None:
        order = collab.create_order_multi(
            self.a,
            9,
            "u",
            "User",
            [{"product_id": self.pc, "quantity": 1}],
            {"id": None, "name": "Cash"},
            "N",
            "Addr",
        )
        self.assertIsNone(order)

        # Single-shop create_order also rejects foreign pid
        order2 = db.create_order(
            self.a,
            9,
            "u",
            "User",
            [{"product_id": self.pb, "quantity": 1}],
            {"id": None, "name": "Cash"},
            "N",
            "Addr",
        )
        self.assertIsNone(order2)

    def test_order_allows_shared_guest_and_tags_owner(self) -> None:
        inv = collab.create_invite(self.a, self.admin, 10)
        collab.accept_invite(inv["token"], self.b, self.admin)
        collab.set_share(self.a, self.b, self.pb, 0)
        order = collab.create_order_multi(
            self.a,
            9,
            "u",
            "User",
            [{"product_id": self.pb, "quantity": 1}],
            {"id": None, "name": "Cash"},
            "N",
            "Addr",
        )
        self.assertIsNotNone(order)
        self.assertEqual(int(order["chat_id"]), self.a)
        items = db.get_order_items(order["id"])
        self.assertEqual(int(items[0]["owner_chat_id"]), self.b)

    def test_clear_inventory_does_not_touch_other_shop(self) -> None:
        with mock.patch.object(db, "OWNER_IDS", {99}):
            ok, _, n = db.clear_shop_inventory(self.a, 99)
        self.assertTrue(ok)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(len(db.list_products(self.a)), 0)
        self.assertEqual(len(db.list_products(self.b)), 1)
        self.assertEqual(len(db.list_products(self.c)), 1)


if __name__ == "__main__":
    unittest.main()
