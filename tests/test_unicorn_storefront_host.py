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

    def test_prefers_unicorn_title_with_stock(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            shop = unicorn_shop.find_catalog_shop()
        self.assertIsNotNone(shop)
        self.assertEqual(int(shop["chat_id"]), UNICORN)

    def test_env_id_wins(self) -> None:
        with mock.patch.dict(os.environ, {"UNICORN_SHOP_CHAT_ID": str(EMPTY)}):
            shop = unicorn_shop.find_catalog_shop()
        self.assertEqual(int(shop["chat_id"]), EMPTY)

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


if __name__ == "__main__":
    unittest.main()
