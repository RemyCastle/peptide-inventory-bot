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
        self.assertTrue(cc.should_merge_prices(8.0, 75.0))  # Snap 8 vial+kit
        self.assertFalse(cc.should_merge_prices(8.0, 50.0))  # Snap 8 250mg SKU
        self.assertFalse(cc.should_merge_prices(10.0, 15.0))  # sibling SKU
        self.assertFalse(cc.should_merge_prices(15.0, 15.0))
        self.assertFalse(cc.should_merge_prices(0, 80))

    def test_generic_shop_title(self) -> None:
        self.assertEqual(
            cc.buyer_shop_title("Shop", unicorn=True),
            cc.DEFAULT_UNICORN_TITLE,
        )
        self.assertEqual(
            cc.buyer_shop_title("Unicorn Magic Factory", unicorn=True),
            "Unicorn Magic Factory",
        )
        self.assertEqual(cc.buyer_shop_title("Shop", unicorn=False), "Shop")


class GlyphRepairTests(unittest.TestCase):
    """Mojibake repair for shop/product text mis-decoded as cp1252/latin-1."""

    def _mojibake(self, s: str, codec: str) -> str:
        # How correct UTF-8 bytes look when a store re-reads them as 8-bit.
        return s.encode("utf-8").decode(codec)

    def test_repairs_emoji_mojibake_both_codecs(self) -> None:
        for original in ("🦄 Welcome to Unicorn Magic Factory 🦄", "🧬 Catalog", "🛒 Cart"):
            for codec in ("cp1252", "latin-1"):
                broken = self._mojibake(original, codec)
                self.assertNotEqual(broken, original)
                self.assertEqual(cc.repair_glyphs(broken), original)

    def test_repairs_accented_latin(self) -> None:
        broken = self._mojibake("Café résumé", "cp1252")
        self.assertEqual(cc.repair_glyphs(broken), "Café résumé")

    def test_leaves_correct_text_untouched(self) -> None:
        for good in ("🦄 Welcome 🦄", "🧬 Catalog", "Anavar 25mg", "café au lait", ""):
            self.assertEqual(cc.repair_glyphs(good), good)

    def test_display_shop_text_repairs_and_keeps_newlines(self) -> None:
        original = "🦄 Welcome!\nBrowse the catalog."
        broken = self._mojibake(original, "cp1252")
        fixed = cc.display_shop_text(broken)
        self.assertEqual(fixed, original)
        self.assertIn("\n", fixed)

    def test_button_label_repairs_and_strips_anavar_and_dollar(self) -> None:
        broken = self._mojibake("🧬 Anav@r 25mg (vial) $15.00", "cp1252")
        label = cc.catalog_button_label(broken, 15.0, 10)
        self.assertIn("🧬", label)  # emoji restored, not mojibake
        self.assertIn("Anavar 25mg", label)
        self.assertNotIn("Anav@r", label)
        self.assertNotIn("ð", label)  # no leftover mojibake glyph
        self.assertEqual(label.count("$15.00"), 1)


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

    def test_hides_same_price_uniqueness_duplicate(self) -> None:
        original = db.add_product(self.shop, "Aod 5mg", 15.0, 10, unit="vial")
        hack = db.add_product(
            self.shop, "Aod 5mg (vial) $15.00", 15.0, 10, unit="vial"
        )
        kit = db.add_product(
            self.shop, "Aod 5mg (vial) $130.00", 130.0, 10, unit="vial"
        )
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, msg, plan = cc.apply_cleanup(
                self.shop, actor_id=self.owner, dry_run=False
            )
        self.assertTrue(ok, msg)
        self.assertEqual(plan.merge_count, 1)
        keeper = db.get_product(original)
        self.assertEqual(keeper["name"], "Aod 5mg")
        self.assertEqual(float(keeper["kit_price"]), 130.0)
        self.assertEqual(int(keeper["active"]), 1)
        self.assertEqual(int(db.get_product(hack)["active"]), 0)
        self.assertEqual(int(db.get_product(kit)["active"]), 0)
        names = [p["name"] for p in db.list_products(self.shop, active_only=True)]
        self.assertEqual(names, ["Aod 5mg"])

    def test_oxytocin_sibling_not_merged_but_dup_hidden(self) -> None:
        lo = db.add_product(self.shop, "Oxytocin", 10.0, 10, unit="vial")
        dup = db.add_product(
            self.shop, "Oxytocin (vial) $10.00", 10.0, 10, unit="vial"
        )
        hi = db.add_product(
            self.shop, "Oxytocin (vial) $30.00", 30.0, 10, unit="vial"
        )
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, _, plan = cc.apply_cleanup(
                self.shop, actor_id=self.owner, dry_run=False
            )
        self.assertTrue(ok)
        self.assertEqual(plan.merge_count, 0)
        self.assertEqual(int(db.get_product(lo)["active"]), 1)
        self.assertEqual(int(db.get_product(dup)["active"]), 0)
        self.assertEqual(int(db.get_product(hi)["active"]), 1)
        self.assertEqual(db.get_product(lo)["name"], "Oxytocin ($10.00)")
        self.assertEqual(db.get_product(hi)["name"], "Oxytocin ($30.00)")
        self.assertIsNone(db.get_product(lo)["kit_price"])

    def test_snap8_250mg_vial_not_merged_into_kit(self) -> None:
        lo = db.add_product(self.shop, "snap 8 (vial) $8.00", 8.0, 10, unit="vial")
        mid = db.add_product(
            self.shop,
            "Snap 8 (vial) $50.00",
            50.0,
            10,
            description="for 250mg",
            unit="vial",
        )
        kit = db.add_product(self.shop, "snap 8 (kit) $75", 75.0, 10, unit="kit")
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            ok, _, plan = cc.apply_cleanup(
                self.shop, actor_id=self.owner, dry_run=False
            )
        self.assertTrue(ok)
        self.assertEqual(plan.merge_count, 1)
        self.assertEqual(int(db.get_product(lo)["active"]), 1)
        self.assertEqual(int(db.get_product(mid)["active"]), 1)
        self.assertEqual(int(db.get_product(kit)["active"]), 0)
        self.assertEqual(float(db.get_product(lo)["kit_price"]), 75.0)
        self.assertEqual(int(db.get_product(lo)["stock"]), 10)
        names = {db.get_product(lo)["name"], db.get_product(mid)["name"]}
        self.assertEqual(len(names), 2)
        self.assertTrue(any("250mg" in n or "$50" in n for n in names))

    def test_mt1_empty_desc_siblings_get_price_suffix(self) -> None:
        a = db.add_product(self.shop, "MT1 (vial) $11.00", 11.0, 10, unit="vial")
        b = db.add_product(self.shop, "MT1 (vial) $30.00", 30.0, 10, unit="vial")
        with mock.patch.object(db, "OWNER_IDS", {self.owner}):
            cc.apply_cleanup(self.shop, actor_id=self.owner, dry_run=False)
        names = {db.get_product(a)["name"], db.get_product(b)["name"]}
        self.assertEqual(names, {"MT1 ($11.00)", "MT1 ($30.00)"})
        self.assertEqual(int(db.get_product(a)["active"]), 1)
        self.assertEqual(int(db.get_product(b)["active"]), 1)

    def test_persist_generic_unicorn_title(self) -> None:
        db.update_shop(self.shop, title="Shop")
        shop = db.get_shop(self.shop)
        got = cc.maybe_persist_unicorn_title(shop)
        self.assertEqual(got, cc.DEFAULT_UNICORN_TITLE)
        self.assertEqual(db.get_shop(self.shop)["title"], cc.DEFAULT_UNICORN_TITLE)
        self.assertIsNone(cc.maybe_persist_unicorn_title(db.get_shop(self.shop)))

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

    def test_open_order_refs_logged_but_still_deactivates(self) -> None:
        lo = db.add_product(self.shop, "Aod 5mg (vial) $15.00", 15.0, 10, unit="vial")
        hi = db.add_product(self.shop, "Aod 5mg (vial) $130.00", 130.0, 10, unit="vial")
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO orders (chat_id, user_id, status, subtotal, total, "
                "created_at, updated_at) VALUES (?, 1, 'pending_payment', 130, 130, "
                "'2026-01-01', '2026-01-01')",
                (self.shop,),
            )
            oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, product_name, "
                "unit_price, quantity, line_total) "
                "VALUES (?, ?, 'Aod 5mg (vial) $130.00', 130, 1, 130)",
                (oid, hi),
            )
        with mock.patch.object(db, "OWNER_IDS", {self.owner}), mock.patch(
            "config.OWNER_IDS", {self.owner}
        ):
            out = cc.apply_bound_shop_cleanup(self.shop, actor_id=self.owner)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["open_order_refs"], 1)
        self.assertEqual(int(db.get_product(hi)["active"]), 0)
        self.assertEqual(int(db.get_product(lo)["active"]), 1)

    def test_pages_catalog_bind_cleans_bound_shop_only(self) -> None:
        import run_cloud
        import unicorn_shop

        db.add_product(self.shop, "Anav@r 25mg", 35.0, 6)
        db.add_product(self.other, "Anav@r 25mg", 35.0, 3)
        with mock.patch.object(db, "OWNER_IDS", {self.owner}), mock.patch(
            "config.OWNER_IDS", {self.owner}
        ), mock.patch.object(
            unicorn_shop, "find_catalog_shop", return_value={"chat_id": self.shop}
        ):
            run_cloud._cleanup_unicorn_catalog()
        self.assertEqual(
            db.list_products(self.shop, active_only=True)[0]["name"],
            "Anavar 25mg",
        )
        self.assertEqual(
            db.list_products(self.other, active_only=True)[0]["name"],
            "Anav@r 25mg",
        )
        import spbc_notify

        recorded = spbc_notify._status_body().get("catalog_cleanup") or {}
        self.assertTrue(recorded.get("ok"))
        self.assertGreaterEqual(int(recorded.get("renames") or 0), 1)

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
