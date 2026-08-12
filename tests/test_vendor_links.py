"""Explicit SPBC→vendor product mapping, and routing that honours it."""

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
import order_router  # noqa: E402
import vendor_links  # noqa: E402

VENDOR = 8100
OTHER = 8200
MASTER = 8300


class VendorLinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "vl.db")
        db.init_db()
        db.ensure_shop(VENDOR, title="Unicorn")
        db.ensure_shop(OTHER, title="Other")
        vendor_links.ensure_tables()
        # Vendor names things her own way
        self.h36 = db.add_product(VENDOR, "H36", 30.0, 150)
        self.foreign = db.add_product(OTHER, "H36", 31.0, 5)
        order_router._pending.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def test_map_and_resolve(self):
        ok, msg = vendor_links.set_link("HGH 360IU", VENDOR, self.h36)
        self.assertTrue(ok, msg)
        got = vendor_links.product_for("HGH 360IU", VENDOR)
        self.assertIsNotNone(got)
        self.assertEqual(got["id"], self.h36)
        # case/spacing insensitive on the SPBC side
        self.assertIsNotNone(vendor_links.product_for("  hgh   360iu ", VENDOR))
        # scoped to the shop it was set for
        self.assertIsNone(vendor_links.product_for("HGH 360IU", OTHER))

    def test_cannot_map_another_shops_product(self):
        ok, msg = vendor_links.set_link("HGH 360IU", VENDOR, self.foreign)
        self.assertFalse(ok)
        self.assertIn("different shop", msg)

    def test_remap_and_clear(self):
        alt = db.add_product(VENDOR, "H36 v2", 32.0, 10)
        vendor_links.set_link("HGH 360IU", VENDOR, self.h36)
        vendor_links.set_link("HGH 360IU", VENDOR, alt)  # upsert, not duplicate
        self.assertEqual(vendor_links.product_for("HGH 360IU", VENDOR)["id"], alt)
        self.assertEqual(len(vendor_links.links_for_shop(VENDOR)), 1)
        self.assertTrue(vendor_links.clear_link("HGH 360IU", VENDOR))
        self.assertIsNone(vendor_links.product_for("HGH 360IU", VENDOR))

    def test_deactivated_product_stops_resolving(self):
        vendor_links.set_link("HGH 360IU", VENDOR, self.h36)
        db.update_product(self.h36, active=0)
        self.assertIsNone(vendor_links.product_for("HGH 360IU", VENDOR))

    def test_catalogs_exclude_own_shop(self):
        db.ensure_shop(MASTER, title="SPBC Shop")
        db.add_product(MASTER, "HGH 360IU", 90.0, 3)
        cats = vendor_links.vendor_catalogs(exclude_shop_id=MASTER)
        ids = {c["shop_chat_id"] for c in cats}
        self.assertIn(VENDOR, ids)
        self.assertNotIn(MASTER, ids)


class RoutingUsesLinksTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "vl2.db")
        db.init_db()
        db.ensure_shop(VENDOR, title="Unicorn")
        vendor_links.ensure_tables()
        self.h36 = db.add_product(VENDOR, "H36", 30.0, 150)
        order_router._pending.clear()
        self._cfg = mock.patch.object(order_router, "SPBC_SHOP_CHAT_ID", 0)
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        self._tmp.cleanup()

    def _payload(self):
        return {
            "order_number": "PEP-LINK-1",
            "status": "paid",
            "items": [{"name": "HGH 360IU (Vial)", "qty": 2}],
            "total_cents": 18000,
        }

    def test_unmapped_name_is_not_quoted(self):
        self.assertEqual(order_router.compute_quotes(self._payload()), [])

    def test_mapped_name_is_quoted_at_vendor_price(self):
        vendor_links.set_link("HGH 360IU", VENDOR, self.h36)
        quotes = order_router.compute_quotes(self._payload())
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["shop_chat_id"], VENDOR)
        self.assertEqual(quotes[0]["total"], 60.0)  # 2 × $30, her price
        self.assertEqual(quotes[0]["breakdown"][0]["name"], "H36")

    def test_mapping_still_respects_stock(self):
        vendor_links.set_link("HGH 360IU", VENDOR, self.h36)
        db.update_product(self.h36, stock=1)
        self.assertEqual(order_router.compute_quotes(self._payload()), [])

    def test_direct_name_match_still_works_without_mapping(self):
        db.add_product(VENDOR, "KPV 10MG", 12.0, 40)
        payload = dict(self._payload())
        payload["items"] = [{"name": "KPV 10MG (Vial)", "qty": 1}]
        quotes = order_router.compute_quotes(payload)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["total"], 12.0)


if __name__ == "__main__":
    unittest.main()
