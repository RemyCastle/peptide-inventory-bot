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

    def test_double_encoded_mojibake(self) -> None:
        original = "🦄 Unicorn Magic Factory"
        once = self._mojibake(original, "cp1252")
        twice = self._mojibake(once, "latin-1")
        self.assertEqual(cc.repair_glyphs(twice), original)
        self.assertEqual(cc.repair_glyphs(once), original)

    def test_curly_quote_mojibake(self) -> None:
        original = "Owner’s shop"
        broken = self._mojibake(original, "cp1252")
        self.assertIn("â", broken)
        self.assertEqual(cc.repair_glyphs(broken), original)

    def test_zwj_emoji_sequence_kept(self) -> None:
        family = "👨\u200d👩\u200d👧\u200d👦"
        self.assertEqual(cc.sanitize_catalog_text(family), family)
        self.assertIn("\u200d", cc.sanitize_catalog_text("A" + family + "\u0000B"))

    def test_zwsp_and_bom_stripped_zwj_kept(self) -> None:
        raw = "AOD\u200b5mg\ufeff \u200d kit"
        out = cc.sanitize_catalog_text(raw)
        self.assertNotIn("\u200b", out)
        self.assertNotIn("\ufeff", out)
        self.assertIn("\u200d", out)

    def test_utf16_len_counts_non_bmp(self) -> None:
        self.assertEqual(cc.utf16_len("A"), 1)
        self.assertEqual(cc.utf16_len("🦄"), 2)
        self.assertEqual(cc.utf16_len("🦄A"), 3)

    def test_clip_label_does_not_split_emoji(self) -> None:
        s = "🦄" * 40
        clipped = cc.clip_label(s, 64)
        self.assertLessEqual(cc.utf16_len(clipped), 64)
        self.assertNotIn("\ufffd", clipped)
        self.assertTrue(clipped.endswith("…") or clipped.endswith("🦄"))
        # Never a lone surrogate / broken emoji
        clipped.encode("utf-16-le")  # would raise on unpaired surrogate

    def test_clip_label_strips_trailing_zwj(self) -> None:
        # 20 BMP chars + ZWJ would otherwise leave a dangling join.
        raw = ("A" * 63) + "\u200d" + "B"
        clipped = cc.clip_label(raw, 64)
        self.assertLessEqual(cc.utf16_len(clipped), 64)
        self.assertFalse(clipped.endswith("\u200d"))

    def test_catalog_button_utf16_cap_with_emoji(self) -> None:
        name = "🦄 " + ("Dermaheal HL anti-hair loss moisturizes scalp 5ml " * 3)
        label = cc.catalog_button_label(name, 15.0, 10)
        self.assertLessEqual(cc.utf16_len(label), cc.TG_BUTTON_MAX)
        self.assertNotIn("\ufffd", label)
        self.assertIn("$15.00", label)

    def test_tg_button_text_repairs_and_caps(self) -> None:
        original = "🦄 Unicorn Magic Factory"
        broken = self._mojibake(original, "cp1252")
        label = cc.tg_button_text(broken + "\n" + ("x" * 80))
        self.assertLessEqual(cc.utf16_len(label), cc.TG_BUTTON_MAX)
        self.assertTrue(label.startswith("🦄"))
        self.assertNotIn("\n", label)

    def test_storefront_label_caps_and_strips(self) -> None:
        self.assertEqual(cc.storefront_label("UMF\u0000-TEE-PK", 40), "UMF-TEE-PK")
        self.assertEqual(cc.storefront_label("X" * 50, 40), "X" * 40)
        self.assertEqual(cc.storefront_label("  "), "")

    def test_repair_leaves_real_latin_and_euro(self) -> None:
        for good in ("10€ off", "Ångström", "café", "œuf", "naïve"):
            self.assertEqual(cc.repair_glyphs(good), good)
            self.assertEqual(cc.sanitize_catalog_text(good), good)

    def test_mixed_ascii_and_emoji_mojibake(self) -> None:
        original = "Hello 🦄 catalog"
        broken = self._mojibake(original, "cp1252")
        self.assertIn("Hello", broken)
        self.assertEqual(cc.repair_glyphs(broken), original)

    def test_emdash_and_ellipsis_mojibake(self) -> None:
        original = "Wait… then — go"
        broken = self._mojibake(original, "cp1252")
        self.assertEqual(cc.repair_glyphs(broken), original)

    def test_flag_and_zwj_sequences_kept(self) -> None:
        flag = "🇺🇸"
        rainbow = "🏳️\u200d🌈"
        wave = "👋🏽"
        for seq in (flag, rainbow, wave):
            self.assertEqual(cc.sanitize_catalog_text(seq), seq)
            self.assertEqual(cc.storefront_label(seq + "\u0000", 40), seq)

    def test_clip_label_keeps_flag_intact(self) -> None:
        flag = "🇺🇸"
        clipped = cc.clip_label(flag * 40, 64)
        self.assertLessEqual(cc.utf16_len(clipped), 64)
        clipped.encode("utf-16-le")
        self.assertNotIn("\ufffd", clipped)

    def test_repair_still_works_when_replacement_char_present(self) -> None:
        original = "🦄 Unicorn Magic Factory"
        broken = self._mojibake(original, "cp1252") + "\ufffd"
        self.assertEqual(cc.repair_glyphs(broken), original)
        self.assertEqual(
            cc.sanitize_catalog_text(broken + "\u0000"),
            original,
        )

    def test_glyph_fixture_table(self) -> None:
        cases = (
            ("🦄", "cp1252"),
            ("🧬 Catalog", "latin-1"),
            ("Owner’s shop", "cp1252"),
            ("Café", "cp1252"),
            ("Wait…", "cp1252"),
            ("A—B", "cp1252"),
        )
        for original, codec in cases:
            with self.subTest(original=original, codec=codec):
                broken = self._mojibake(original, codec)
                self.assertEqual(cc.repair_glyphs(broken), original)

    def test_buyer_shop_title_strips_junk(self) -> None:
        self.assertEqual(
            cc.buyer_shop_title("Unicorn\u200b Magic Factory"),
            "Unicorn Magic Factory",
        )

    def test_cleanup_source_never_deletes_products(self) -> None:
        src = (ROOT / "catalog_cleanup.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"(?i)DELETE\s+FROM\s+products")
        self.assertNotRegex(src, r"(?i)DROP\s+TABLE")
        self.assertNotRegex(src, r"(?i)unlink\s*\(")
        cloud = (ROOT / "run_cloud.py").read_text(encoding="utf-8")
        self.assertNotRegex(cloud, r"(?i)os\.remove\(.*inventory")
        self.assertNotRegex(cloud, r"(?i)unlink\(.*inventory")

    def test_bom_does_not_block_mojibake_repair(self) -> None:
        original = "🦄 Unicorn Magic Factory"
        broken = "\ufeff" + self._mojibake(original, "cp1252")
        self.assertEqual(cc.repair_glyphs(broken), original)
        self.assertEqual(cc.sanitize_catalog_text(broken), original)

    def test_triple_encoded_mojibake(self) -> None:
        original = "🦄"
        once = self._mojibake(original, "cp1252")
        twice = self._mojibake(once, "latin-1")
        thrice = self._mojibake(twice, "latin-1")
        self.assertEqual(cc.repair_glyphs(thrice), original)

    def test_replacement_only_becomes_empty(self) -> None:
        self.assertEqual(cc.repair_glyphs("\ufffd\ufffd"), "")
        self.assertEqual(cc.sanitize_catalog_text("\ufffd"), "")
        self.assertEqual(cc.storefront_label("\u0000\ufffd", 40), "")

    def test_keycap_and_vs16_kept(self) -> None:
        keycap = "1\ufe0f\u20e3"
        umbrella = "\u2602\ufe0f"
        self.assertEqual(cc.sanitize_catalog_text(keycap), keycap)
        self.assertEqual(cc.sanitize_catalog_text(umbrella), umbrella)
        clipped = cc.clip_label(umbrella + " extra", 2, ellipsis="")
        self.assertEqual(clipped, umbrella)
        self.assertIn("\ufe0f", clipped)

    def test_clip_label_drops_lone_regional_indicator(self) -> None:
        flag = "🇺🇸"
        # 10 flags = 40 units. Budget 7 with ellipsis → 6 units = 1 flag + 1 RI.
        clipped = cc.clip_label(flag * 10, 7)
        self.assertLessEqual(cc.utf16_len(clipped), 7)
        ris = [ch for ch in clipped if cc._is_regional_indicator(ch)]
        self.assertEqual(len(ris) % 2, 0)
        clipped.encode("utf-16-le")

    def test_clip_label_strips_zwj_when_it_fits(self) -> None:
        raw = ("A" * 63) + "\u200d" + "B"
        clipped = cc.clip_label(raw, 64, ellipsis="")
        self.assertFalse(clipped.endswith("\u200d"))
        self.assertLessEqual(cc.utf16_len(clipped), 64)

    def test_display_shop_text_strips_nul_keeps_newline(self) -> None:
        raw = "Hello\u0000\nWorld\ufffd"
        out = cc.display_shop_text(raw)
        self.assertEqual(out, "Hello\nWorld")
        self.assertNotIn("\u0000", out)

    def test_public_shipping_zones_skips_junk_only_id(self) -> None:
        self.assertIsNone(
            cc.public_shipping_zones(
                [{"id": "\u0000\ufffd", "label": "Nope", "fee": 1, "free_above": 0}]
            )
        )
        zones = cc.public_shipping_zones(
            [
                {"id": "\u0000", "label": "Bad", "fee": 1, "free_above": 0},
                {"id": "US-W\u200b", "label": "West\ufffd", "fee": 8, "free_above": 0},
            ]
        )
        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["id"], "US-W")
        self.assertEqual(zones[0]["label"], "West")

    def test_sanitize_multiline_clips_without_splitting_emoji(self) -> None:
        raw = "🦄" * 40
        out = cc.sanitize_multiline(raw, 10)
        self.assertLessEqual(cc.utf16_len(out), 10)
        out.encode("utf-16-le")
        self.assertNotIn("\ufffd", out)

    def test_subdivision_flag_kept(self) -> None:
        england = (
            "\U0001F3F4\U000E0067\U000E0062\U000E0065"
            "\U000E006E\U000E0067\U000E007F"
        )
        self.assertEqual(cc.sanitize_catalog_text(england), england)
        self.assertEqual(cc.storefront_label(england + "\u0000", 40), england)
        self.assertIn("\U000e007f", cc.sanitize_catalog_text(england))

    def test_clip_label_keeps_complete_subdivision_flag(self) -> None:
        england = (
            "\U0001F3F4\U000E0067\U000E0062\U000E0065"
            "\U000E006E\U000E0067\U000E007F"
        )
        clipped = cc.clip_label(england + " West", 64)
        self.assertTrue(clipped.startswith(england))
        self.assertIn("\U000e007f", clipped)
        clipped.encode("utf-16-le")

    def test_clip_label_drops_incomplete_tag_run(self) -> None:
        england = (
            "\U0001F3F4\U000E0067\U000E0062\U000E0065"
            "\U000E006E\U000E0067\U000E007F"
        )
        # Flag is 14 UTF-16 units. Budget 10 with no ellipsis cuts mid-tags.
        clipped = cc.clip_label(england, 10, ellipsis="")
        self.assertLessEqual(cc.utf16_len(clipped), 10)
        self.assertFalse(any(cc._is_emoji_tag(ch) for ch in clipped))
        clipped.encode("utf-16-le")

    def test_rlo_and_c1_controls_dropped(self) -> None:
        raw = "AB\u202eCD\u0080EF"
        out = cc.sanitize_catalog_text(raw)
        self.assertEqual(out, "ABCDEF")
        self.assertNotIn("\u202e", out)
        self.assertNotIn("\u0080", out)

    def test_nfd_combining_accent_survives(self) -> None:
        nfd = "Cafe\u0301"
        out = cc.sanitize_catalog_text(nfd)
        self.assertNotIn("\ufffd", out)
        self.assertTrue("é" in out or "Cafe" in out)

    def test_public_http_url_uppercase_and_junk(self) -> None:
        self.assertEqual(
            cc.public_http_url("HTTPS://cdn.example.com/p.png"),
            "https://cdn.example.com/p.png",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/p\u0000.png\ufffd"),
            "https://cdn.example.com/p.png",
        )
        self.assertEqual(cc.public_http_url("javascript:alert(1)"), "")
        self.assertEqual(cc.public_http_url("https://cdn.example.com/p.png%00.jpg"), "")
        self.assertEqual(cc.public_http_url("https://cdn.example.com/p.png%0d%0aX"), "")
        self.assertEqual(cc.public_http_url(""), "")
        self.assertEqual(cc.public_http_url("ftp://x"), "")

    def test_mixed_real_flag_and_mojibake_latin(self) -> None:
        flag = "🇺🇸"
        broken = self._mojibake(" West", "cp1252")
        out = cc.sanitize_catalog_text(flag + broken)
        self.assertTrue(out.startswith(flag))
        self.assertIn("West", out)
        self.assertNotIn("ð", out)

    def test_line_sep_and_private_use_dropped(self) -> None:
        out = cc.sanitize_catalog_text("A\u2028B\u2029C\uE000D")
        self.assertEqual(out, "A B CD")
        self.assertNotIn("\u2028", out)
        self.assertNotIn("\uE000", out)
        welcome = cc.display_shop_text("Hi\u2028there")
        self.assertIn("\n", welcome)
        self.assertNotIn("\u2028", welcome)

    def test_noncharacter_and_leading_combining_dropped(self) -> None:
        self.assertEqual(cc.sanitize_catalog_text("AB\uFFFECD"), "ABCD")
        self.assertEqual(cc.sanitize_catalog_text("\u0301Cafe"), "Cafe")
        self.assertEqual(cc.sanitize_catalog_text("\ufe0f Hello"), "Hello")
        self.assertEqual(
            cc.sanitize_catalog_text("\U0001F3FB wave"),
            "wave",
        )

    def test_incomplete_subdivision_flag_stripped_when_it_fits(self) -> None:
        incomplete = "\U0001F3F4\U000E0067\U000E0062"
        self.assertNotIn("\U000e0067", cc.sanitize_catalog_text(incomplete))
        clipped = cc.clip_label(incomplete + " West", 64, ellipsis="")
        self.assertFalse(any(cc._is_emoji_tag(ch) for ch in clipped))
        clipped.encode("utf-16-le")

    def test_public_http_url_rejects_breaks_userinfo_and_schemes(self) -> None:
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/p.png\r\nSet-Cookie: x"),
            "",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/p.png%09.jpg"),
            "",
        )
        self.assertEqual(
            cc.public_http_url("https://user:pass@cdn.example.com/p.png"),
            "",
        )
        self.assertEqual(cc.public_http_url("data:text/html,hi"), "")
        self.assertEqual(cc.public_http_url("vbscript:msg"), "")
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/ok.png\u2028x"),
            "",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/ok.png"),
            "https://cdn.example.com/ok.png",
        )

    def test_force_reply_cap_helper_keeps_emoji(self) -> None:
        label = cc.tg_button_text("🦄 " + ("x" * 80), 64)
        self.assertLessEqual(cc.utf16_len(label), 64)
        self.assertTrue(label.startswith("🦄"))
        label.encode("utf-16-le")

    def test_invisible_fillers_dropped(self) -> None:
        raw = "A\uFFFCB\u2800C\u3164D\uFFA0E"
        out = cc.sanitize_catalog_text(raw)
        self.assertEqual(out, "ABCDE")
        self.assertNotIn("\uFFFC", out)
        self.assertNotIn("\u2800", out)
        self.assertNotIn("\u3164", out)
        self.assertNotIn("\uFFA0", out)
        welcome = cc.display_shop_text("Hi\u2800 there")
        self.assertEqual(welcome, "Hi there")
        self.assertEqual(cc.sanitize_catalog_text("A\u1160B"), "AB")

    def test_pirate_flag_and_profession_zwj_kept(self) -> None:
        pirate = "🏴\u200d☠️"
        coder = "👩\u200d💻"
        self.assertEqual(cc.sanitize_catalog_text(pirate), pirate)
        self.assertEqual(cc.sanitize_catalog_text(coder), coder)
        self.assertIn("\u200d", cc.sanitize_catalog_text(pirate + "\u0000"))
        clipped = cc.clip_label(pirate + " West", 64)
        self.assertTrue(clipped.startswith(pirate))
        clipped.encode("utf-16-le")

    def test_bidi_isolates_dropped(self) -> None:
        raw = "AB\u2066CD\u2069EF"
        out = cc.sanitize_catalog_text(raw)
        self.assertEqual(out, "ABCDEF")
        self.assertNotIn("\u2066", out)
        self.assertNotIn("\u2069", out)

    def test_public_http_url_rejects_percent_decoded_controls(self) -> None:
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/ok%E2%80%A8x"),
            "",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/ok%E2%80%AEx"),
            "",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/ok%2500.jpg"),
            "",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/ok%C0%80.jpg"),
            "",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/p%20.png"),
            "https://cdn.example.com/p%20.png",
        )
        self.assertEqual(
            cc.public_http_url("https://cdn.example.com/%F0%9F%A6%8C.png"),
            "https://cdn.example.com/%F0%9F%A6%8C.png",
        )


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
