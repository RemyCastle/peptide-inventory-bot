"""Catalog cleanup: names, vial/kit merge, never-delete."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import catalog_cleanup as cc  # noqa: E402
import db  # noqa: E402


class NameCleanTests(unittest.TestCase):
    def test_strips_price_and_unit_suffix(self) -> None:
        self.assertEqual(cc.clean_product_name("Aod 5mg (vial) $15.00"), "Aod 5mg")
        self.assertEqual(cc.clean_product_name("AICAR 50mg (kit)"), "AICAR 50mg")
        self.assertEqual(cc.clean_product_name("BPC 20mg (vial)"), "BPC 20mg")

    def test_anavar_typo(self) -> None:
        self.assertEqual(cc.clean_product_name("Anav@r 25mg"), "Anavar 25mg")
        self.assertEqual(cc.clean_product_name("anav@r 25mg"), "Anavar 25mg")

    def test_plain_name_unchanged(self) -> None:
        self.assertEqual(cc.clean_product_name("SEMA 10MG"), "SEMA 10MG")

    def test_strips_replacement_and_nbsp(self) -> None:
        raw = "AOD\u00a05mg\ufffd (vial) $15.00"
        self.assertEqual(cc.clean_product_name(raw), "AOD 5mg")
        self.assertEqual(cc.sanitize_catalog_text("A\u200bB\u0000C"), "ABC")
        self.assertNotIn("\ufffd", cc.sanitize_catalog_text("X\ufffdY"))

    def test_nfkc_fullwidth_dollar_then_price_tail(self) -> None:
        # Fullwidth $ becomes ASCII $ under NFKC, then uniqueness tail strips.
        self.assertEqual(cc.clean_product_name("AOD 5mg ＄15.00"), "AOD 5mg")

    def test_display_name_never_empty(self) -> None:
        self.assertEqual(cc.display_product_name(""), "Item")
        self.assertEqual(
            cc.display_product_name("Anav@r 25mg"),
            "Anavar 25mg",
        )

    def test_button_label_fits_telegram_cap(self) -> None:
        long_name = (
            "Dermaheal HL anti-hair loss, moisturizes and nourishes "
            "hair and scalp 5ml (vial) $15.00"
        )
        label = cc.catalog_button_label(long_name, 15.0, 10)
        self.assertLessEqual(len(label), cc.TG_BUTTON_MAX)
        self.assertIn("$15.00", label)
        self.assertNotIn("\ufffd", label)
        self.assertFalse(label.endswith("(vial)"))

    def test_button_label_strips_jammed_price_so_it_is_not_doubled(self) -> None:
        label = cc.catalog_button_label("Aod 5mg (vial) $15.00", 15.0, 10)
        self.assertEqual(label.count("$15.00"), 1)
        self.assertTrue(label.startswith("Aod 5mg"))
        self.assertIn("10 left", label)

    def test_button_label_out_of_stock(self) -> None:
        label = cc.catalog_button_label("SEMA 10MG", 60.0, 0)
        self.assertLessEqual(len(label), cc.TG_BUTTON_MAX)
        self.assertIn("(out)", label)

    def test_md_escape_underscore(self) -> None:
        self.assertEqual(cc.md_escape("RET_A 2mg"), "RET\\_A 2mg")

    def test_kit_ratio(self) -> None:
        self.assertTrue(cc.should_merge_prices(15.0, 130.0))
        self.assertTrue(cc.should_merge_prices(10.0, 80.0))
        self.assertFalse(cc.should_merge_prices(10.0, 15.0))  # sibling SKU
        self.assertFalse(cc.should_merge_prices(15.0, 15.0))
        self.assertFalse(cc.should_merge_prices(0, 80))


class CleanupApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "clean.db")
        db.init_db()
        self.shop = 44001
        self.other = 44002
        self.owner = 111
        db.ensure_shop(self.shop, title="Unicorn Magic Factory")
        db.ensure_shop(self.other, title="Other")
        db.add_product(self.other, "Keep Me", 9.0, 4)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_merges_vial_dollar_pair_as_kit(self) -> None:
        lo = db.add_product(self.shop, "Aod 5mg (vial) $15.00", 15.0, 10, unit="vial")
        hi = db.add_product(self.shop, "Aod 5mg (vial) $130.00", 130.0, 10, unit="vial")
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, msg, plan = cc.apply_cleanup(self.shop, actor_id=self.owner, dry_run=False)
        self.assertTrue(ok, msg)
        self.assertEqual(plan.merge_count, 1)
        self.assertEqual(plan.deactivate_count, 1)
        keeper = db.get_product(lo)
        loser = db.get_product(hi)
        self.assertEqual(keeper["name"], "Aod 5mg")
        self.assertEqual(float(keeper["kit_price"]), 130.0)
        self.assertEqual(int(keeper["stock"]), 10)
        self.assertEqual(int(keeper["active"]), 1)
        self.assertEqual(int(loser["active"]), 0)
        # Other shop untouched
        self.assertEqual(len(db.list_products(self.other, active_only=False)), 1)

    def test_merges_explicit_kit_unit(self) -> None:
        vial = db.add_product(self.shop, "AICAR 50mg (vial)", 13.0, 8, unit="vial")
        kit = db.add_product(self.shop, "AICAR 50mg (kit)", 100.0, 8, unit="kit")
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, msg, _ = cc.apply_cleanup(self.shop, actor_id=self.owner, dry_run=False)
        self.assertTrue(ok, msg)
        keeper = db.get_product(vial)
        self.assertEqual(keeper["name"], "AICAR 50mg")
        self.assertEqual(float(keeper["kit_price"]), 100.0)
        self.assertEqual(int(db.get_product(kit)["active"]), 0)

    def test_does_not_merge_sibling_sku(self) -> None:
        a = db.add_product(
            self.shop,
            "B12 5ml vial (vial) $10.00",
            10.0,
            10,
            description="(cyano) .5mg/ml",
            unit="vial",
        )
        b = db.add_product(
            self.shop,
            "B12 5ml vial (vial) $15.00",
            15.0,
            10,
            description="hydroxycolabin 2mg/ml",
            unit="vial",
        )
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, _, plan = cc.apply_cleanup(self.shop, actor_id=self.owner, dry_run=False)
        self.assertTrue(ok)
        self.assertEqual(plan.merge_count, 0)
        self.assertEqual(plan.deactivate_count, 0)
        self.assertEqual(int(db.get_product(a)["active"]), 1)
        self.assertEqual(int(db.get_product(b)["active"]), 1)
        names = {db.get_product(a)["name"], db.get_product(b)["name"]}
        self.assertTrue(all("$" not in n for n in names))
        self.assertEqual(len(names), 2)

    def test_anavar_rename_only(self) -> None:
        pid = db.add_product(self.shop, "Anav@r 25mg", 35.0, 6)
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            cc.apply_cleanup(self.shop, actor_id=self.owner, dry_run=False)
        self.assertEqual(db.get_product(pid)["name"], "Anavar 25mg")
        self.assertEqual(int(db.get_product(pid)["stock"]), 6)

    def test_never_deletes(self) -> None:
        lo = db.add_product(self.shop, "cag 5 (vial) $17.00", 17.0, 10)
        hi = db.add_product(self.shop, "cag 5 (vial) $150.00", 150.0, 10)
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            cc.apply_cleanup(self.shop, actor_id=self.owner, dry_run=False)
        self.assertIsNotNone(db.get_product(lo))
        self.assertIsNotNone(db.get_product(hi))
        with db.get_db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM products WHERE chat_id = ?",
                (self.shop,),
            ).fetchone()["c"]
        self.assertEqual(n, 2)

    def test_dry_run_does_not_write(self) -> None:
        pid = db.add_product(self.shop, "Anav@r 25mg", 35.0, 6)
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, msg, plan = cc.apply_cleanup(self.shop, actor_id=self.owner, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("preview", msg.lower())
        self.assertGreaterEqual(plan.rename_count, 1)
        self.assertEqual(db.get_product(pid)["name"], "Anav@r 25mg")

    def test_non_owner_denied(self) -> None:
        db.add_product(self.shop, "Anav@r 25mg", 35.0, 6)
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, msg, _ = cc.apply_cleanup(self.shop, actor_id=999, dry_run=False)
        self.assertFalse(ok)
        self.assertIn("owner", msg.lower())
        self.assertEqual(
            db.list_products(self.shop)[0]["name"], "Anav@r 25mg"
        )


if __name__ == "__main__":
    unittest.main()
