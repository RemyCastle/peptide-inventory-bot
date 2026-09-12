"""get_bot_tokens_for_shop merges main-bot tokens for the Unicorn catalog shop.

When the main SPBC process is itself the Unicorn customer bot (or
UNICORN_BOT_TOKEN is unset), Mini App initData is signed by a main-pool token
and there is no VENDOR_STORES_JSON entry. These tests pin that the main pool
(TELEGRAM_BOT_TOKEN + BOT_TOKENS) is allowed to verify Unicorn checkout, and
only for the Unicorn shop. Scratch DB only — never touches inventory.db.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import db  # noqa: E402
import spbc_notify  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

MAIN_TOKEN = "111111111:MAIN-telegram-bot-token"
POOL_TOKEN = "222222222:POOL-standby-token"
UNICORN_SHOP = 90001
OTHER_SHOP = 90002


class UnicornBotTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "unicorn_tokens.db")
        db.init_db()
        db.ensure_shop(UNICORN_SHOP, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER_SHOP, title="Some Other Peptide Shop")
        # Isolate from laptop .env: no vendor JSON, merge is the only source.
        self._patches = [
            mock.patch.object(vendor_stores, "load_vendor_configs", return_value=[]),
            mock.patch.object(config, "TELEGRAM_BOT_TOKEN", MAIN_TOKEN),
            mock.patch.object(
                config, "resolve_bot_tokens", return_value=[MAIN_TOKEN, POOL_TOKEN]
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_unicorn_shop_includes_main_pool_tokens(self) -> None:
        tokens = vendor_stores.get_bot_tokens_for_shop(UNICORN_SHOP)
        self.assertIn(MAIN_TOKEN, tokens)
        self.assertIn(POOL_TOKEN, tokens)

    def test_main_token_added_even_when_pool_omits_it(self) -> None:
        # BOT_TOKENS pool without TELEGRAM_BOT_TOKEN: single token must still
        # be merged (resolve_bot_tokens drops it once the pool is non-empty).
        with mock.patch.object(
            config, "resolve_bot_tokens", return_value=[POOL_TOKEN]
        ):
            tokens = vendor_stores.get_bot_tokens_for_shop(UNICORN_SHOP)
        self.assertIn(MAIN_TOKEN, tokens)
        self.assertIn(POOL_TOKEN, tokens)

    def test_non_unicorn_shop_gets_no_main_tokens(self) -> None:
        tokens = vendor_stores.get_bot_tokens_for_shop(OTHER_SHOP)
        self.assertNotIn(MAIN_TOKEN, tokens)
        self.assertNotIn(POOL_TOKEN, tokens)

    def test_tokens_deduped(self) -> None:
        tokens = vendor_stores.get_bot_tokens_for_shop(UNICORN_SHOP)
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_stale_shop_pin_still_merges_unicorn_env_tokens(self) -> None:
        """UNICORN_BOT_TOKEN + UNICORN_EXTRA apply even if shop_chat_id is wrong."""
        stale = "333333333:STALE-unicorn"
        extra = "444444444:UNICORN-EXTRA"
        env = {
            "TELEGRAM_BOT_TOKEN": MAIN_TOKEN,
            "BOT_TOKENS": POOL_TOKEN,
            "UNICORN_BOT_TOKEN": stale,
            "UNICORN_EXTRA_BOT_TOKENS": extra,
            "UNICORN_SHOP_CHAT_ID": str(OTHER_SHOP),
            "VENDOR_STORES_JSON": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            tokens = vendor_stores.get_bot_tokens_for_shop(UNICORN_SHOP)
        self.assertIn(MAIN_TOKEN, tokens)
        self.assertIn(POOL_TOKEN, tokens)
        self.assertIn(stale, tokens)
        self.assertIn(extra, tokens)

    def test_initdata_error_code_mapping(self) -> None:
        self.assertEqual(
            vendor_stores.initdata_error_code(
                vendor_stores.InitDataError("expired auth_date", hash_ok=True)
            ),
            "expired",
        )
        self.assertEqual(
            vendor_stores.initdata_error_code(vendor_stores.InitDataError("bad hash")),
            "bad_hash",
        )
        self.assertEqual(
            vendor_stores.initdata_error_code(
                vendor_stores.InitDataError("missing initData or bot token")
            ),
            "bad_hash",
        )


def _sign_init_data(bot_token: str, user_id: int = 66001) -> str:
    auth = int(time.time())
    user = {
        "id": int(user_id),
        "first_name": "Buyer",
        "last_name": "Bee",
        "username": "buyer_user",
        "language_code": "en",
    }
    pairs = {
        "auth_date": str(auth),
        "query_id": "AAEAAAE_unicorn_main",
        "user": json.dumps(user, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    pairs["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urllib.parse.urlencode(pairs)


class UnicornMainTokenOrderTests(unittest.TestCase):
    """POST /order accepts initData signed by TELEGRAM_BOT_TOKEN for Unicorn."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "unicorn_order.db")
        db.init_db()
        db.ensure_shop(UNICORN_SHOP, title="Unicorn Magic Factory")
        webpanel.ensure_webpanel_tables()
        self.sf_key = webpanel._ensure_storefront_key(UNICORN_SHOP)
        self.pid = db.add_product(UNICORN_SHOP, "BPC-157 5MG", 40.0, stock=20)
        db.add_payment_method(UNICORN_SHOP, "Venmo", "@shop-venmo memo CODE")
        self._patches = [
            mock.patch.object(vendor_stores, "load_vendor_configs", return_value=[]),
            mock.patch.object(config, "TELEGRAM_BOT_TOKEN", MAIN_TOKEN),
            mock.patch.object(
                config, "resolve_bot_tokens", return_value=[MAIN_TOKEN, POOL_TOKEN]
            ),
            mock.patch.object(
                vendor_stores, "base_notify_ids_for_shop", return_value=[]
            ),
            mock.patch.object(
                vendor_stores,
                "vendor_meta_for_shop",
                return_value={
                    "name": "Unicorn Magic Factory",
                    "emoji": "\U0001f984",
                    "notify_ids": [],
                },
            ),
            mock.patch.object(
                webpanel, "telegram_send_with_token", return_value=True
            ),
            mock.patch.object(
                webpanel, "telegram_send_photo_with_token", return_value=True
            ),
            mock.patch.object(spbc_notify, "send_telegram", return_value={}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _payload(self, bot_token: str) -> dict:
        return {
            "invite": self.sf_key,
            "initData": _sign_init_data(bot_token),
            "items": [{"id": self.pid, "vials": 1, "kits": 0}],
            "ship": {
                "name": "Buyer Bee",
                "line1": "1 Test St",
                "city": "Austin",
                "state": "TX",
                "zip": "78701",
                "phone": "512-555-0100",
            },
        }

    def test_telegram_bot_token_initdata_200(self) -> None:
        code, body = spbc_notify.handle_http_order(self._payload(MAIN_TOKEN))
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))

    def test_bot_tokens_pool_entry_initdata_200(self) -> None:
        code, body = spbc_notify.handle_http_order(self._payload(POOL_TOKEN))
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))

    def test_unrelated_token_still_401(self) -> None:
        code, body = spbc_notify.handle_http_order(
            self._payload("999999999:not-a-shop-token")
        )
        self.assertEqual(code, 401)
        self.assertEqual(body.get("error"), "bad_hash")

    def test_unicorn_extra_token_initdata_200(self) -> None:
        extra = "444444444:UNICORN-EXTRA"
        env = {
            "UNICORN_BOT_TOKEN": "333333333:STALE-unicorn",
            "UNICORN_EXTRA_BOT_TOKENS": extra,
            "VENDOR_STORES_JSON": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            code, body = spbc_notify.handle_http_order(self._payload(extra))
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))

    def test_empty_initdata_still_401(self) -> None:
        payload = self._payload(MAIN_TOKEN)
        payload["initData"] = ""
        code, body = spbc_notify.handle_http_order(payload)
        self.assertEqual(code, 401)
        self.assertEqual(body.get("error"), "bad_hash")


if __name__ == "__main__":
    unittest.main()
