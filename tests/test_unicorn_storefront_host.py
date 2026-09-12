"""Live Unicorn Mini App catalog key binds to unicornfartzz-bot, not supplier-bot."""

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
import run_cloud  # noqa: E402
import unicorn_shop  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

UNICORN = 81001
OTHER = 81002
EMPTY = 81003
PAGES_KEY = unicorn_shop.PAGES_STOREFRONT_KEY
BUYER = 91001


class PagesKeyTests(unittest.TestCase):
    def test_default_matches_pages_html(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_STOREFRONT_KEY", None)
            self.assertEqual(unicorn_shop.pages_storefront_key(), PAGES_KEY)
            self.assertTrue(unicorn_shop.is_pages_storefront_key(PAGES_KEY))
            self.assertTrue(unicorn_shop.is_pages_storefront_key("vendor" + PAGES_KEY))
            self.assertFalse(unicorn_shop.is_pages_storefront_key("aa" * 12))

    def test_env_override(self) -> None:
        other = "aaaaaaaaaaaaaaaaaaaaaaaa"
        with mock.patch.dict(os.environ, {"UNICORN_STOREFRONT_KEY": other}):
            self.assertEqual(unicorn_shop.pages_storefront_key(), other)
            self.assertTrue(unicorn_shop.is_pages_storefront_key(other))
            self.assertFalse(unicorn_shop.is_pages_storefront_key(PAGES_KEY))


class FindCatalogShopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "catalog.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="@unicornmagicfactory")
        db.ensure_shop(OTHER, title="Other Vendor")
        db.ensure_shop(EMPTY, title="Unicorn Fancy Empty")
        db.add_product(UNICORN, "BPC-157 5MG", 40.0, stock=12)
        db.add_product(OTHER, "Other Vial", 9.0, stock=3)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_brand_group_title_is_not_unicorn(self) -> None:
        self.assertFalse(
            unicorn_shop.shop_title_looks_unicorn(
                "Ash, UnicornFartzzBot and Samantha"
            )
        )
        self.assertTrue(
            unicorn_shop.shop_title_looks_unicorn("Unicorn Magic Factory")
        )

    def test_empty_brand_group_does_not_steal_stocked_shop(self) -> None:
        brand = 81099
        db.ensure_shop(brand, title="Ash, UnicornFartzzBot and Samantha")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            shop = unicorn_shop.find_catalog_shop()
        self.assertEqual(int(shop["chat_id"]), UNICORN)
        self.assertNotEqual(int(shop["chat_id"]), brand)

    def test_prefers_unicorn_title_with_stock(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            shop = unicorn_shop.find_catalog_shop()
        self.assertIsNotNone(shop)
        self.assertEqual(int(shop["chat_id"]), UNICORN)

    def test_env_id_wins_when_stocked(self) -> None:
        extra = 81005
        db.ensure_shop(extra, title="Pinned Unicorn")
        db.add_product(extra, "Pinned Vial", 11.0, stock=2)
        with mock.patch.dict(os.environ, {"UNICORN_SHOP_CHAT_ID": str(extra)}):
            shop = unicorn_shop.find_catalog_shop()
        self.assertEqual(int(shop["chat_id"]), extra)

    def test_empty_env_pin_falls_through_to_stock(self) -> None:
        with mock.patch.dict(os.environ, {"UNICORN_SHOP_CHAT_ID": str(EMPTY)}):
            shop = unicorn_shop.find_catalog_shop()
        self.assertEqual(int(shop["chat_id"]), UNICORN)

    def test_stocked_untitled_shop_beats_empty_ash_group(self) -> None:
        db.ensure_shop(81099, title="Ash, UnicornFartzzBot and Samantha")
        # UNICORN is titled + stocked in setUp
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            shop = unicorn_shop.find_catalog_shop()
        self.assertEqual(int(shop["chat_id"]), UNICORN)
        self.assertGreater(
            unicorn_shop._product_count(int(shop["chat_id"])), 0
        )

    def test_newest_paid_unicorn_wins_among_titled(self) -> None:
        extra = 81004
        db.ensure_shop(extra, title="Unicorn Magic Factory")
        db.add_product(extra, "RETA 8MG", 80.0, stock=4)
        pid = db.list_products(extra, active_only=True)[0]["id"]
        order = db.create_order(
            extra,
            BUYER,
            "buyer",
            "Buyer",
            [
                {
                    "product_id": pid,
                    "product_name": "RETA 8MG",
                    "unit_price": 80.0,
                    "quantity": 1,
                }
            ],
            {"id": None, "name": "Venmo"},
            "Buyer",
            "1 St",
            "",
        )
        self.assertIsNotNone(order)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE orders SET status = 'paid', paid_at = '2026-09-11T00:00:00Z' "
                "WHERE id = ?",
                (order["id"],),
            )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            shop = unicorn_shop.find_catalog_shop()
        self.assertEqual(int(shop["chat_id"]), extra)


class StorefrontBindTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "sf.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.add_product(UNICORN, "Klow 80mg", 150.0, stock=7)
        webpanel.ensure_webpanel_tables()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pages_key_resolves_without_prior_row(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            os.environ.pop("UNICORN_STOREFRONT_KEY", None)
            sid = webpanel.resolve_storefront_key(PAGES_KEY)
        self.assertEqual(sid, UNICORN)
        code, body = webpanel.api_storefront(PAGES_KEY)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertGreater(len(body["products"]), 0)
        self.assertEqual(body["products"][0]["name"], "Klow 80mg")

    def test_unknown_key_still_404(self) -> None:
        code, body = webpanel.api_storefront("ffffffffffffffffffffffff")
        self.assertEqual(code, 404)
        self.assertEqual(body.get("error"), "unknown storefront")
        self.assertIn("Re-open", body.get("message") or "")

    def test_ensure_plain_then_boot_bind(self) -> None:
        webpanel.ensure_storefront_key_plain(UNICORN, PAGES_KEY)
        self.assertEqual(webpanel.resolve_storefront_key(PAGES_KEY), UNICORN)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            run_cloud._bind_unicorn_pages_storefront()
        code, body = webpanel.api_storefront(PAGES_KEY)
        self.assertEqual(code, 200, body)
        self.assertEqual(len(body["products"]), 1)

    def test_reassign_key_from_other_shop(self) -> None:
        db.ensure_shop(OTHER, title="Wrong")
        webpanel.ensure_storefront_key_plain(OTHER, PAGES_KEY)
        webpanel.ensure_storefront_key_plain(UNICORN, PAGES_KEY)
        self.assertEqual(webpanel.resolve_storefront_key(PAGES_KEY), UNICORN)


class VendorResolveAndTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "vr.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.add_product(UNICORN, "BPC-157", 40.0, stock=2)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolve_shop_without_invite(self) -> None:
        v = {"name": "Unicorn Magic Factory", "token": "1:ABC"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            sid = vendor_stores._resolve_shop(v)
        self.assertEqual(sid, UNICORN)

    def test_same_magicfactory2_token_skips_second_poller(self) -> None:
        tok = "123456:MAGICFACTORY2"
        with mock.patch(
            "config.TELEGRAM_BOT_TOKEN", tok
        ), mock.patch(
            "config.resolve_bot_tokens", return_value=[tok]
        ):
            self.assertTrue(vendor_stores._token_already_polled_by_main(tok))
            self.assertFalse(vendor_stores._token_already_polled_by_main("other:token"))

    def test_customer_bot_flag(self) -> None:
        env = {"PUBLIC_BOT_USERNAME": "UnicornMagicFactory2Bot", "UNICORN_BOT_TOKEN": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(unicorn_shop.is_unicorn_customer_bot())


class HealthHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "health.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.add_product(UNICORN, "BPC-157", 40.0, stock=3)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_health_points_at_unicornfartzz(self) -> None:
        import spbc_notify

        body = spbc_notify._status_body()
        self.assertEqual(body["service"], "unicornfartzz-bot")
        self.assertEqual(
            body["storefront_host"], "https://unicornfartzz-bot.onrender.com"
        )

    def test_health_true_from_bound_catalog_shop(self) -> None:
        import spbc_notify

        with mock.patch.object(spbc_notify, "SUPPLIER_TELEGRAM_CHAT_ID", ""), \
             mock.patch.object(spbc_notify, "OWNER_TELEGRAM_CHAT_ID", ""):
            body = spbc_notify._status_body()
        self.assertTrue(body["default_chat_configured"])
        self.assertTrue(body["owner_chat_configured"])
        self.assertEqual(body["payments"]["active"], 0)
        self.assertEqual(body["payments"]["total"], 0)

    def test_health_exposes_render_git_sha_and_cleanup(self) -> None:
        import spbc_notify

        spbc_notify.set_catalog_cleanup_result(None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RENDER_GIT_COMMIT", None)
            os.environ.pop("GIT_COMMIT", None)
            body = spbc_notify._status_body()
        self.assertNotIn("git_sha", body)
        self.assertNotIn("catalog_cleanup", body)
        with mock.patch.dict(
            os.environ, {"RENDER_GIT_COMMIT": "a" * 40}, clear=False
        ):
            spbc_notify.set_catalog_cleanup_result(
                {
                    "ok": True,
                    "renames": 2,
                    "merges": 1,
                    "deactivated": 3,
                    "shop_chat_id": 999,
                    "msg": "Catalog cleanup applied on this shop.",
                }
            )
            body = spbc_notify._status_body()
        self.assertEqual(body["git_sha"], "a" * 40)
        self.assertEqual(
            body["catalog_cleanup"],
            {
                "ok": True,
                "renames": 2,
                "merges": 1,
                "deactivated": 3,
                "msg": "Catalog cleanup applied on this shop.",
            },
        )
        self.assertNotIn("shop_chat_id", body["catalog_cleanup"])
        spbc_notify.set_catalog_cleanup_result(None)

    def test_dockerfile_copies_catalog_cleanup(self) -> None:
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("catalog_cleanup.py", text)

    def test_boot_cleanup_applies_when_owner_ids_unset(self) -> None:
        """Render Unicorn often has empty OWNER_IDS; boot must still clean."""
        import catalog_cleanup as cc
        import run_cloud

        db.add_product(UNICORN, "Anav@r 25mg", 35.0, 6)
        db.add_product(UNICORN, "Aod 5mg (vial) $15.00", 15.0, 8)
        with mock.patch("config.OWNER_IDS", set()), mock.patch.object(
            db, "OWNER_IDS", set()
        ), mock.patch.object(
            unicorn_shop, "find_catalog_shop", return_value={"chat_id": UNICORN}
        ):
            out = cc.apply_bound_shop_cleanup(UNICORN)
            run_cloud._cleanup_unicorn_catalog()
        self.assertTrue(out["ok"], out)
        names = [p["name"] for p in db.list_products(UNICORN, active_only=True)]
        self.assertTrue(names)
        self.assertTrue(all("Anav@r" not in n and "$" not in n for n in names))
        recorded = __import__("spbc_notify")._status_body().get("catalog_cleanup") or {}
        self.assertTrue(recorded.get("ok"))

    def test_boot_cleanup_renames_generic_shop_title(self) -> None:
        import run_cloud

        db.update_shop(UNICORN, title="Shop")
        with mock.patch("config.OWNER_IDS", set()), mock.patch.object(
            db, "OWNER_IDS", set()
        ), mock.patch.object(
            unicorn_shop,
            "find_catalog_shop",
            return_value={"chat_id": UNICORN, "title": "Shop"},
        ):
            run_cloud._cleanup_unicorn_catalog()
        self.assertEqual(
            db.get_shop(UNICORN)["title"], "Unicorn Magic Factory"
        )
        recorded = __import__("spbc_notify")._status_body().get("catalog_cleanup") or {}
        self.assertEqual(recorded.get("shop_title"), "Unicorn Magic Factory")


MINIAPP_HTML = Path(r"C:\Users\Remy\projects\miniapp-demos\unicorn\index.html")


class MiniAppPagesContractTests(unittest.TestCase):
    """Pages HTML (other folder) must consume structured checkout rails."""

    @classmethod
    def setUpClass(cls) -> None:
        if not MINIAPP_HTML.is_file():
            raise unittest.SkipTest("Pages Mini App HTML is not on this machine")
        cls.src = MINIAPP_HTML.read_text(encoding="utf-8")

    def test_public_key_matches_bot(self) -> None:
        self.assertIn(PAGES_KEY, self.src)
        self.assertIn("unicornfartzz-bot.onrender.com", self.src)

    def test_uses_structured_pay_rails(self) -> None:
        self.assertIn("payment_methods", self.src)
        self.assertIn("pay_url", self.src)
        self.assertIn("pay_hint", self.src)
        self.assertIn("checkout_ready", self.src)
        self.assertIn("checkout_message", self.src)
        self.assertIn("renderPayMethods", self.src)
        self.assertIn("updateCheckoutCopy", self.src)
        self.assertIn("order-paid", self.src)
        self.assertIn("can_mark_paid", self.src)
        self.assertIn("I've paid", self.src)
        self.assertIn("copytarget", self.src)

    def test_drops_mockup_copy(self) -> None:
        low = self.src.lower()
        self.assertNotIn("sample data for this mockup", low)
        self.assertNotIn("make-believe", low)
        self.assertNotIn("mini app concept", low)
        self.assertNotIn("synced by fairy bot 12s ago", low)


if __name__ == "__main__":
    unittest.main()
