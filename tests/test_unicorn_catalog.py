"""Unicorn Magic Factory catalog UX: categories, names, copy, art. Never wipe."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import unicorn_catalog as uc  # noqa: E402
import webpanel  # noqa: E402

UNICORN = 91001
OTHER = 91002

# Live /storefront names as of 2026-09-12. Frozen so mapping stays honest.
LIVE_NAMES: tuple[tuple[str, str], ...] = (
    ("10ct slide case for 2/3ml vials", "Accessories"),
    ("3ml case holds 20 vials", "Accessories"),
    ("3ml case holds 25 vials", "Accessories"),
    ("3ml, 10ml flexi caps", "Accessories"),
    ("5 amino 1 100mg/ml 10ml", "Metabolic"),
    ("5a1 100mg", "Metabolic"),
    ("5a1 50mg", "Metabolic"),
    ("5a1 tabs", "Tabs"),
    ("7ct slide case for pen carts", "Accessories"),
    ("8mg methylprednisolone 14 tabs", "Tabs"),
    ("AICAR 50mg", "Healing"),
    ("AOD 2mg", "Metabolic"),
    ("AOD water 10ml", "BAC-water"),
    ("ARA 290 10mg", "Healing"),
    ("ARA 290 16mg", "Healing"),
    ("ARA 290 30mg", "Healing"),
    ("ATP-S inj", "Vitality"),
    ("Acetic Acid 10ml", "BAC-water"),
    ("Adamax 10mg", "Cognition"),
    ("Adamax 20mg", "Cognition"),
    ("Adrenaline 1mg/ml 1ml ampoule", "Vitality"),
    ("AhkCu", "Skin"),
    ("Alcarnitine 10ml", "Metabolic"),
    ("Anavar 25mg", "Tabs"),
    ("Aod 5mg", "Metabolic"),
    ("B12 10ml vial", "Vitality"),
    ("B12 5ml vial", "Vitality"),
    ("B12 5ml vial (hydroxycolabin 2mg/ml)", "Vitality"),
    ("B12 ampules (1ml)", "Vitality"),
    ("B12 ampules (2ml)", "Vitality"),
    ("BPC 157", "Healing"),
    ("BPC 20mg", "Healing"),
    ("BPC 5", "Healing"),
    ("BPC/Tb4 10/10", "Healing"),
    ("BPC/kpv capsules 100 caps for 100 doses/100 days supply", "Tabs"),
    ("BPC/tb frag 10/10", "Healing"),
    ("BPC/tb4 5/5", "Healing"),
    ("Bac Saline 20ml", "BAC-water"),
    ("Bac Water 10ml", "BAC-water"),
    ("Bac Water 2ml", "BAC-water"),
    ("Bac water (Pfizer/Hospira) 30ml", "BAC-water"),
    ("Bac with vial spike holder (single)", "Accessories"),
    ("Bpc 10mg", "Healing"),
    ("Cag 10", "GLP-1"),
    ("Cardiogen 20mg", "Longevity"),
    ("Cartalax 20mg", "Longevity"),
    ("Chioctocin (thioctic acid) 5ml ampule", "Vitality"),
    ("Chonluten 20mg", "Longevity"),
    ("Cjc + ipa 10/10", "Metabolic"),
    ("Cjc + ipa 5/5", "Metabolic"),
    ("Cjc no dac 10mg", "Metabolic"),
    ("Cjc no dac 5mg", "Metabolic"),
    ("Cortagen 20mg", "Longevity"),
    ("Crystagen 20 mg", "Longevity"),
    ("DSIP", "Cognition"),
    ("DSIP 5mg", "Cognition"),
    ("Derma roller (64 needles) for microneedling", "Accessories"),
    ("Dermaheal HL anti-hair loss, moisturizes and nourishes hair and scalp 5ml", "Skin"),
    ("Dsip 10mg", "Cognition"),
    ("Dsip 15mg", "Cognition"),
    ("Elasty filler", "Skin"),
    ("Elora 10", "Vitality"),
    ("Epitalon 40mg", "Longevity"),
    ("Epitalon 50mg", "Longevity"),
    ("Facial cream with tallow, ghkcu and methylene Blue", "Skin"),
    ("Fat Blaster 10ml", "Metabolic"),
    ("Fox04 DRI 10", "Longevity"),
    ("GHKcu", "Skin"),
    ("Ghk basic (white, no copper)", "Skin"),
    ("GhkCu 100mg", "Skin"),
    ("Ghkcu 80mg", "Skin"),
    ("Ginko Biloba 17.5mg/5ml ampoules", "Vitality"),
    ("HCG 5000iu", "Vitality"),
    ("Hair Growth Serum - Biotin & Kopyrrol Supplies rich nutrition to scalp and hair, boosting the hair growth 7ml", "Skin"),
    ("Hair Luma - a scalp booster made with exosomes, PDRN DNA repair. 5ml", "Skin"),
    ("Hair skin nails 10ml", "Skin"),
    ("Hey girl hey 13.5iu", "Metabolic"),
    ("Hey girl hey 17iu", "Metabolic"),
    ("Hey girl hey 24iu", "Metabolic"),
    ("Hgh Fragment 176–191 5mg", "Metabolic"),
    ("Hishiphagen C", "Healing"),
    ("Hmg 75iu", "Vitality"),
    ("IGF DES 2mg", "Metabolic"),
    ("IGF-1LR3 1mg", "Metabolic"),
    ("IPA 10mg", "Metabolic"),
    ("IPA 5mg", "Metabolic"),
    ("Immunity Blend 10ml", "Healing"),
    ("Injection pen", "Accessories"),
    ("Joint support 10ml", "Healing"),
    ("KPV 10mg", "Healing"),
    ("KPV 30mg", "Healing"),
    ("Kabelline (fat dissolver) Contouring Serum", "Skin"),
    ("Kisspeptin", "Vitality"),
    ("Kisspeptin 10mg", "Vitality"),
    ("Kisspeptin 5mg", "Vitality"),
    ("Klow 80mg", "Skin"),
    ("LL37 5mg", "Healing"),
    ("Lady test 50mg/ml 10ml", "Vitality"),
    ("Laennec Placenta injection", "Healing"),
    ("Lcarn + MIC blend 10ml", "Metabolic"),
    ("Lcarn 20ml 600mg/ml", "Metabolic"),
    ("Lemon Bottle (fat dissolver)", "Metabolic"),
    ("Lidocaine 2% inj 20ml", "Vitality"),
    ("Lipo c focus 10ml", "Metabolic"),
    ("Lipo c with b12 10ml", "Metabolic"),
    ("Lipo c without b12 10ml", "Metabolic"),
    ("Livagen 20mg", "Longevity"),
    ("MT1 ($11.00)", "Vitality"),
    ("MT1 ($30.00)", "Vitality"),
    ("MT2 ($11.00)", "Vitality"),
    ("MT2 ($30.00)", "Vitality"),
    ("Matrixyl", "Skin"),
    ("Maxiblue 5- Supply of trace elements (zinc, copper, manganese, selenium, chromium)", "Vitality"),
    ("Maz12", "GLP-1"),
    ("Mini pen cartridge purge blocks (purge in the middle, holds 4 pen carts)", "Accessories"),
    ("Mots 40mg", "Longevity"),
    ("Mots c 10mg", "Longevity"),
    ("MotsC 30mg", "Longevity"),
    ("Muchcaine lidocaine cream-10.56mg 30g/1oz tube", "Vitality"),
    ("Multivitamin (A, D3, E, Thiamin hydrochorate, pyridoxine hydrochloride, riboflavin sodium phosphate, nicotinamide, as...", "Vitality"),
    ("NA Selank 50mg", "Cognition"),
    ("NA Semax 50mg", "Cognition"),
    ("NAD+ 1000mg", "Longevity"),
    ("NAD+ 500", "Longevity"),
    ("NMN Nad+ skin booster contains Adenosine, PDRN, Niacinamide, Nicotinamide Mononucleotide 5ml vial", "Skin"),
    ("Needless Scalp serum applicator-for use to apply serums to scalp", "Accessories"),
    ("Ocean pharma Bac 30ml (tested similar to Hospira and a great substitute)", "BAC-water"),
    ("Oral tada tincture", "Tabs"),
    ("Ovagen 20mg", "Longevity"),
    ("Oxytocin ($10.00)", "Vitality"),
    ("Oxytocin ($30.00)", "Vitality"),
    ("P21", "Cognition"),
    ("P21 (with edamatane)", "Cognition"),
    ("PBS (phosphate buffer solution)", "BAC-water"),
    ("PE 22-28", "Cognition"),
    ("PE 22-28 8mg", "Cognition"),
    ("PGB NAD+ 485 apx", "Longevity"),
    ("PT141", "Vitality"),
    ("PT141 10mg", "Vitality"),
    ("Pancragen 20mg", "Longevity"),
    ("Peach Bottle (fat dissolver)", "Metabolic"),
    ("Peach Bottle skin booster", "Skin"),
    ("Pen (assorted colors)", "Accessories"),
    ("Pen cartridges", "Accessories"),
    ("Pen carts", "Accessories"),
    ("Pen noodles", "Accessories"),
    ("Pen noodles (needles)", "Accessories"),
    ("Pens", "Accessories"),
    ("Pine Bottle (fat dissolver)", "Metabolic"),
    ("Pinealon", "Cognition"),
    ("Pinealon 10mg", "Cognition"),
    ("Prednisone 10mg", "Tabs"),
    ("Prednisone 20mg", "Tabs"),
    ("Prednisone 5mg", "Tabs"),
    ("Prostamax 20mg", "Longevity"),
    ("Recovery blend 10ml", "Healing"),
    ("Reduced Glutathione 1200mg", "Longevity"),
    ("Reduced Glutathione 1500mg", "Longevity"),
    ("Reduced Glutathione 600mg", "Longevity"),
    ("Relax blend PM 10ml", "Cognition"),
    ("Reta 10", "Retatrutide"),
    ("Reta 15", "Retatrutide"),
    ("Reta 20", "Retatrutide"),
    ("Reta 30", "Retatrutide"),
    ("Reta 46", "Retatrutide"),
    ("Reta 50", "Retatrutide"),
    ("Reta 60", "Retatrutide"),
    ("Reta 70", "Retatrutide"),
    ("SS31 10mg", "Healing"),
    ("SS31 25mg", "Healing"),
    ("Selank 10mg", "Cognition"),
    ("Selank 22mg", "Cognition"),
    ("Selank/Semax blend 10/10", "Cognition"),
    ("Sema 10mg", "GLP-1"),
    ("Sema 30", "GLP-1"),
    ("Sema 5mg", "GLP-1"),
    ("Semax", "Cognition"),
    ("Semax & Selank blend", "Cognition"),
    ("Semax 10mg", "Cognition"),
    ("Semax 30mg", "Cognition"),
    ("Sermorelin 10mg", "Metabolic"),
    ("Sermorelin 5mg", "Metabolic"),
    ("Shred 10ml", "Metabolic"),
    ("Single vial container for 10ml", "Accessories"),
    ("Single vial container for 3ml", "Accessories"),
    ("Single vial container for Gluta", "Accessories"),
    ("Sleep blend 10ml", "Cognition"),
    ("SluPP 50 capsules 22mg", "Tabs"),
    ("SluPP oral tincture", "Metabolic"),
    ("Slupp injectible 7.5mg/ml 10ml", "Metabolic"),
    ("Sodium bicarbonate (to raise ph or “buffer” nad, ARA)", "BAC-water"),
    ("Ss31 30mg", "Healing"),
    ("Sterile saline for tox (single use)", "BAC-water"),
    ("Super shredder 10ml", "Metabolic"),
    ("Superhuman 10ml", "Cognition"),
    ("Survo 10", "Vitality"),
    ("Synake", "Skin"),
    ("TA1 10mg", "Healing"),
    ("Tb500 (tb4) 10mg", "Healing"),
    ("Tesa + IPA 10/10", "Metabolic"),
    ("Tesa 10", "Metabolic"),
    ("Test E 250mg/ml 10ml", "Vitality"),
    ("Test cyp 250mg/ml 10ml", "Vitality"),
    ("Testagen 20mg", "Longevity"),
    ("Thymogen 20mg", "Healing"),
    ("Thymulin", "Healing"),
    ("Tirz 10", "GLP-1"),
    ("Tirz 15", "GLP-1"),
    ("Tirz 20", "GLP-1"),
    ("Tirz 40", "GLP-1"),
    ("Tirz 5", "GLP-1"),
    ("Tirz 60", "GLP-1"),
    ("Tret .05", "Skin"),
    ("Tret.025", "Skin"),
    ("Unicorn Tears (Cagri water ph 4.5)", "BAC-water"),
    ("VIP", "Cognition"),
    ("VIP 5mg", "Cognition"),
    ("Vesilute 20mg", "Longevity"),
    ("Vesugen 20mg", "Longevity"),
    ("Vilon 20mg", "Longevity"),
    ("Vit C 20ml vial", "Vitality"),
    ("Vit D ampules (1ml)", "Vitality"),
    ("Vitamin B complex (B1, B2, B3, B6, B7, B12) 2ml ampoule", "Vitality"),
    ("Zinc Sulfate Hydrate 10ml", "Vitality"),
    ("bc (levonorgestral/ethinyloestradial)", "Tabs"),
    ("cag 5", "GLP-1"),
    ("epitalon 10mg", "Longevity"),
    ("ghkcu 50mg", "Skin"),
    ("glow 70mg", "Skin"),
    ("mots 20mg", "Longevity"),
    ("plan b", "Tabs"),
    ("snap 8", "Skin"),
    ("tirz 30", "GLP-1"),
    ("tirz100", "GLP-1"),
)


class CategorizeTests(unittest.TestCase):
    def test_every_live_sku_maps_honestly(self) -> None:
        bad = []
        for name, want in LIVE_NAMES:
            got = uc.categorize(name)
            if got != want:
                bad.append((name, want, got))
        self.assertEqual(bad, [], msg=bad[:12])

    def test_no_other_junk_bucket(self) -> None:
        self.assertNotIn("Other", uc.CATEGORY_IDS)
        for name, _want in LIVE_NAMES:
            self.assertIn(uc.categorize(name), uc.CATEGORY_IDS)

    def test_sema_does_not_steal_semax(self) -> None:
        self.assertEqual(uc.categorize("Sema 10mg"), "GLP-1")
        self.assertEqual(uc.categorize("Semax 10mg"), "Cognition")
        self.assertEqual(uc.categorize("NA Semax 50mg"), "Cognition")

    def test_peach_bottle_skin_vs_fat(self) -> None:
        self.assertEqual(uc.categorize("Peach Bottle skin booster"), "Skin")
        self.assertEqual(uc.categorize("Peach Bottle (fat dissolver)"), "Metabolic")

    def test_aod_water_vs_aod(self) -> None:
        self.assertEqual(uc.categorize("AOD water 10ml"), "BAC-water")
        self.assertEqual(uc.categorize("Aod 5mg"), "Metabolic")


class PrettyNameTests(unittest.TestCase):
    def test_title_case_and_typos(self) -> None:
        self.assertEqual(uc.pretty_name("tirz100"), "Tirz 100")
        self.assertEqual(uc.pretty_name("snap 8"), "Snap-8")
        self.assertEqual(uc.pretty_name("plan b"), "Plan B")
        self.assertEqual(uc.pretty_name("cag 5"), "Cagri 5")
        self.assertIn("Needleless", uc.pretty_name("Needless Scalp serum applicator-for use to apply serums to scalp"))
        self.assertIn("Hydroxocobalamin", uc.pretty_name("B12 5ml vial (hydroxycolabin 2mg/ml)"))
        self.assertIn("Injectable", uc.pretty_name("Slupp injectible 7.5mg/ml 10ml"))

    def test_strips_price_tail(self) -> None:
        self.assertEqual(uc.pretty_name("MT1 ($11.00)"), "MT1")
        self.assertEqual(uc.pretty_name("Aod 5mg (vial) $15.00"), "AOD 5mg")

    def test_no_replacement_junk(self) -> None:
        self.assertNotIn("\ufffd", uc.pretty_name("AOD\ufffd 5mg"))
        self.assertTrue(uc.pretty_name("AOD 5mg")[0].isupper())

    def test_description_has_no_medical_claims(self) -> None:
        banned = ("treat", "cure", "diagnos", "prescription", "weight loss", "heal your")
        for name, cat in LIVE_NAMES[:40]:
            desc = uc.item_description(name, cat).casefold()
            for word in banned:
                self.assertNotIn(word, desc, msg=name)


class ApplyUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "ux.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER, title="Other Vendor")
        self.sema = db.add_product(UNICORN, "sema 10mg", 110.0, 6, description="")
        self.reta = db.add_product(UNICORN, "reta 30", 28.0, 4)
        self.mt_a = db.add_product(UNICORN, "MT1 ($11.00)", 11.0, 3)
        self.mt_b = db.add_product(UNICORN, "MT1 ($30.00)", 30.0, 3)
        self.other = db.add_product(OTHER, "sema 10mg", 99.0, 8)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_apply_does_not_change_prices_or_other_shop(self) -> None:
        out = uc.apply_catalog_ux(UNICORN)
        self.assertTrue(out["ok"], out)
        sema = db.get_product(self.sema)
        self.assertEqual(float(sema["price"]), 110.0)
        self.assertEqual(int(sema["stock"]), 6)
        self.assertEqual(sema["category"], "GLP-1")
        self.assertEqual(sema["name"], "Sema 10mg")
        self.assertTrue(sema["description"])
        self.assertIn("/catalog-img/", sema["photo_file_id"] or "")
        reta = db.get_product(self.reta)
        self.assertEqual(reta["category"], "Retatrutide")
        self.assertEqual(float(reta["price"]), 28.0)
        other = db.get_product(self.other)
        self.assertEqual(other["name"], "sema 10mg")
        self.assertIsNone(other.get("category"))
        self.assertEqual(float(other["price"]), 99.0)

    def test_sibling_mt1_keeps_both_prices(self) -> None:
        uc.apply_catalog_ux(UNICORN)
        a = db.get_product(self.mt_a)
        b = db.get_product(self.mt_b)
        self.assertEqual(float(a["price"]), 11.0)
        self.assertEqual(float(b["price"]), 30.0)
        self.assertEqual(int(a["active"] or 0), 1)
        self.assertEqual(int(b["active"] or 0), 1)
        self.assertEqual(a.get("variant_group"), b.get("variant_group"))
        self.assertNotEqual(a.get("variant_label"), b.get("variant_label"))

    def test_idempotent_second_pass(self) -> None:
        uc.apply_catalog_ux(UNICORN)
        again = uc.apply_catalog_ux(UNICORN)
        self.assertEqual(again["renames"], 0)
        self.assertEqual(again["categories"], 0)

    def test_never_deletes_in_source(self) -> None:
        src = (ROOT / "unicorn_catalog.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"(?i)DELETE\s+FROM\s+products")
        self.assertNotRegex(src, r"(?i)DROP\s+TABLE")
        self.assertNotRegex(src, r"(?i)os\.remove\(.*inventory")

    def test_storefront_enriches_unicorn_only(self) -> None:
        webpanel.ensure_webpanel_tables()
        key = webpanel._ensure_storefront_key(UNICORN)
        code, body = webpanel.api_storefront(key)
        self.assertEqual(code, 200, body)
        by_id = {p["id"]: p for p in body["products"]}
        self.assertEqual(by_id[self.sema]["price"], 110.0)
        self.assertEqual(by_id[self.sema]["category"], "GLP-1")
        self.assertTrue(by_id[self.sema]["description"])
        self.assertTrue(by_id[self.sema]["photo_url"].startswith("http"))
        ids = {c["id"] for c in body["categories"]}
        self.assertIn("GLP-1", ids)
        self.assertIn("Retatrutide", ids)
        other_key = webpanel._ensure_storefront_key(OTHER)
        code2, body2 = webpanel.api_storefront(other_key)
        self.assertEqual(code2, 200, body2)
        self.assertNotIn("categories", body2)
        self.assertEqual(body2["products"][0]["name"], "sema 10mg")
        self.assertEqual(body2["products"][0]["price"], 99.0)


class ImagePackTests(unittest.TestCase):
    def test_packaged_slugs_exist(self) -> None:
        for slug in set(uc.CATEGORY_IMAGE.values()) | {
            "blend",
            "pen",
            "cream",
            "capsules",
            "ampoule",
            "tabs",
            "bac",
        }:
            self.assertIsNotNone(uc.catalog_image_path(slug), msg=slug)

    def test_read_rejects_traversal(self) -> None:
        self.assertIsNone(uc.read_catalog_image("../bot.py"))
        self.assertIsNone(uc.read_catalog_image("foo.png"))
        data = uc.read_catalog_image("glp1.jpg")
        self.assertIsNotNone(data)
        self.assertGreater(len(data[0]), 1000)
        self.assertEqual(data[1], "image/jpeg")

    def test_image_slug_varies(self) -> None:
        slugs = {uc.image_slug(n, c) for n, c in LIVE_NAMES}
        self.assertGreaterEqual(len(slugs), 8)


if __name__ == "__main__":
    unittest.main()
