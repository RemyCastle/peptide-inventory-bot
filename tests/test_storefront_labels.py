"""Storefront label sanitizer audit: every buyer-facing field is cleaned."""

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
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP = 91001
USER = 42
BUYER = 66011


def _mojibake(s: str, codec: str = "cp1252") -> str:
    return s.encode("utf-8").decode(codec)


class StorefrontLabelAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "labels.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Unicorn Magic Factory")
        webpanel.ensure_webpanel_tables()
        self.tok = {"chat_id": SHOP, "user_id": USER}
        self.sf_key = webpanel._ensure_storefront_key(SHOP)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _insert_dirty_product(
        self,
        name: str,
        *,
        sku: str | None = None,
        category: str | None = None,
        variant_group: str | None = None,
        variant_label: str | None = None,
        price: float = 10.0,
        stock: int = 3,
    ) -> int:
        """Bypass add_product sanitizer so the READ path has to clean."""
        now = db._utc_now()
        with db.get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO products
                  (chat_id, name, description, price, stock, unit, active,
                   sort_order, sku, category, variant_group, variant_label,
                   created_at, updated_at)
                VALUES (?, ?, '', ?, ?, 'vial', 1, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SHOP,
                    name,
                    price,
                    stock,
                    sku,
                    category,
                    variant_group,
                    variant_label,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def test_storefront_repairs_dirty_name_sku_category_variant(self) -> None:
        dirty_name = _mojibake("🧬 Anav@r 25mg (vial) $15.00")
        dirty_sku = "UMF\u0000-TEE\ufffd-PK"
        dirty_cat = _mojibake("Peptides 🧬")
        self._insert_dirty_product(
            dirty_name,
            sku=dirty_sku,
            category=dirty_cat,
            variant_group="tee\u200b",
            variant_label="Pink\ufffd",
        )
        code, body = webpanel.api_storefront(self.sf_key)
        self.assertEqual(code, 200, body)
        self.assertEqual(len(body["products"]), 1)
        p = body["products"][0]
        self.assertEqual(p["name"], "🧬 Anavar 25mg")
        self.assertNotIn("Anav@r", p["name"])
        self.assertNotIn("$15", p["name"])
        self.assertNotIn("\ufffd", p["name"])
        self.assertEqual(p["sku"], "UMF-TEE-PK")
        self.assertEqual(p["variant_group"], "tee")
        self.assertEqual(p["variant_label"], "Pink")
        self.assertIn("Peptides", p["category"] or "")
        self.assertIn("🧬", p["category"] or "")
        self.assertNotIn("ð", p["category"] or "")

    def test_storefront_payment_names_sanitized(self) -> None:
        db.add_payment_method(SHOP, "Venmo\u0000\ufffd", "@wineboos")
        code, body = webpanel.api_storefront(self.sf_key)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["payments"], ["Venmo"])
        self.assertEqual(body["payment_methods"][0]["name"], "Venmo")

    def test_storefront_shop_title_mojibake(self) -> None:
        db.update_shop(SHOP, title=_mojibake("🦄 Unicorn Magic Factory"))
        with mock.patch("unicorn_shop.is_unicorn_shop", return_value=True):
            code, body = webpanel.api_storefront(self.sf_key)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["shop"]["title"], "🦄 Unicorn Magic Factory")

    def test_order_status_item_name_sanitized(self) -> None:
        pid = self._insert_dirty_product("Aod 5mg (vial) $15.00")
        db.add_payment_method(SHOP, "Venmo", "@x")
        order = db.create_order(
            SHOP,
            BUYER,
            "buyer",
            "Buyer",
            [
                {
                    "product_id": pid,
                    "product_name": "Aod 5mg (vial) $15.00",
                    "unit_price": 15.0,
                    "quantity": 1,
                }
            ],
            {"id": None, "name": "Venmo"},
            "Buyer",
            "1 St",
            "",
        )
        self.assertIsNotNone(order)
        code, body = webpanel.api_order_status(
            self.sf_key, order["payment_code"]
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["items"][0]["name"], "Aod 5mg")

    def test_admin_write_sku_strips_junk(self) -> None:
        pid = db.add_product(SHOP, "Tee", 25.0, 4)
        code, _ = webpanel.api_product(
            self.tok,
            {
                "id": pid,
                "sku": "UMF\u0000-TEE-PK\ufffd",
                "variant_label": "Pink\u200b",
                "category": "Merch\ufeff",
            },
        )
        self.assertEqual(code, 200)
        row = db.get_product(pid)
        self.assertEqual(row["sku"], "UMF-TEE-PK")
        self.assertEqual(row["variant_label"], "Pink")
        self.assertEqual(row["category"], "Merch")

    def test_payment_method_public_sanitizes_name(self) -> None:
        mid = db.add_payment_method(SHOP, "PayPal\ufffd", "friends")
        methods = db.list_payment_methods(SHOP, active_only=True)
        row = next(m for m in methods if int(m["id"]) == mid)
        pub = vendor_stores.payment_method_public(row, 10.0, "ABC123")
        self.assertEqual(pub["name"], "PayPal")
        self.assertNotIn("\ufffd", pub["line"])

    def test_zwj_family_survives_storefront_name(self) -> None:
        family = "👨\u200d👩\u200d👧\u200d👦 Kit"
        self._insert_dirty_product(family)
        code, body = webpanel.api_storefront(self.sf_key)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["products"][0]["name"], family)

    def test_control_only_sku_becomes_none(self) -> None:
        self._insert_dirty_product("SEMA 10MG", sku="\u0000\ufffd\u200b")
        code, body = webpanel.api_storefront(self.sf_key)
        self.assertEqual(code, 200, body)
        self.assertIsNone(body["products"][0]["sku"])

    def test_scratch_db_only_never_touches_repo_inventory(self) -> None:
        repo_db = ROOT / "inventory.db"
        current = Path(db._db_path).resolve()
        self.assertTrue(str(current).endswith("labels.db"))
        self.assertNotEqual(current, repo_db.resolve())


class LabelFuzzTests(unittest.TestCase):
    """Cheap coverage: mixed junk never produces replacement glyphs or oversize."""

    def test_fuzz_sanitize_and_clip(self) -> None:
        junk = (
            "\u0000\u0007\u200b\ufeff\ufffd\u00ad"
            + _mojibake("🦄")
            + " Anav@r 25mg (vial) $15.00 "
            + ("🧬" * 20)
        )
        cleaned = cc.sanitize_catalog_text(junk)
        self.assertNotIn("\ufffd", cleaned)
        self.assertNotIn("\u0000", cleaned)
        label = cc.catalog_button_label(junk, 15.0, 4)
        self.assertLessEqual(cc.utf16_len(label), cc.TG_BUTTON_MAX)
        self.assertNotIn("\ufffd", label)
        self.assertIn("Anavar", label)
        clipped = cc.clip_label("🦄" * 80, 64)
        clipped.encode("utf-8")
        clipped.encode("utf-16-le")
        self.assertLessEqual(cc.utf16_len(clipped), 64)


if __name__ == "__main__":
    unittest.main()
