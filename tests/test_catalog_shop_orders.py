"""Catalog-shop /orders, keep-only reattach, Mini App staff notify (scratch DB)."""

from __future__ import annotations

import asyncio
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
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import run_cloud  # noqa: E402
import spbc_notify  # noqa: E402
import unicorn_shop  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

CATALOG = 22001
PERSONAL = 11001
OTHER = 33001
OWNER = 88001
ADMIN = 88002
BUYER = 77001
VENDOR_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def _run(coro):
    return asyncio.run(coro)


def build_valid_init_data(
    bot_token: str,
    *,
    user_id: int = BUYER,
    username: str = "buyer_user",
    first_name: str = "Buyer",
    last_name: str = "Bee",
    auth_date: int | None = None,
) -> str:
    auth = int(auth_date if auth_date is not None else time.time())
    user = {
        "id": int(user_id),
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "language_code": "en",
    }
    pairs: dict[str, str] = {
        "auth_date": str(auth),
        "query_id": "AAEAAAE_test_query",
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


class CatalogShopOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "catalog_orders.db")
        db.init_db()
        webpanel.ensure_webpanel_tables()
        db.ensure_shop(CATALOG, title="Unicorn Magic Factory")
        db.ensure_shop(PERSONAL, title="Shop")
        db.ensure_shop(OTHER, title="Other Vendor")
        db.add_product(CATALOG, "BPC-157 5MG", 40.0, stock=10)
        db.add_product(OTHER, "Other Vial", 9.0, stock=3)
        db.add_admin(CATALOG, ADMIN, "ghostie", OWNER)
        self.pid = db.list_products(CATALOG, active_only=True)[0]["id"]
        self.order = db.create_order(
            CATALOG,
            BUYER,
            "iphone_buyer",
            "Buyer Bee",
            [{"product_id": self.pid, "quantity": 1}],
            None,
            "Buyer Bee",
            "1 Test St",
            "via mini app",
        )
        self.assertIsNotNone(self.order)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_staff_shop_prefers_catalog(self) -> None:
        with mock.patch.object(db, "OWNER_IDS", {OWNER}):
            shop = unicorn_shop.staff_shop_for_admin(OWNER)
        self.assertIsNotNone(shop)
        self.assertEqual(int(shop["chat_id"]), CATALOG)
        shop_a = unicorn_shop.staff_shop_for_admin(ADMIN)
        self.assertEqual(int(shop_a["chat_id"]), CATALOG)
        self.assertIsNone(unicorn_shop.staff_shop_for_admin(BUYER))

    def test_recent_snapshot_lists_miniapp_order(self) -> None:
        snap = unicorn_shop.recent_orders_snapshot(10)
        self.assertTrue(snap)
        row = snap[0]
        self.assertEqual(int(row["id"]), int(self.order["id"]))
        self.assertEqual(row["payment_code"], self.order["payment_code"])
        self.assertEqual(int(row["chat_id"]), CATALOG)
        self.assertEqual(row["status"], "pending_payment")
        self.assertEqual(row["username"], "iphone_buyer")

    def test_keep_only_reattaches_stray_without_deleting(self) -> None:
        personal_pid = db.add_product(PERSONAL, "Stray Vial", 11.0, stock=2)
        stray = db.create_order(
            PERSONAL,
            BUYER,
            "iphone_buyer",
            "Buyer Bee",
            [{"product_id": personal_pid, "quantity": 1}],
            None,
            "Buyer Bee",
            "1 Test St",
            "via mini app",
        )
        self.assertIsNotNone(stray)
        other_pid = db.list_products(OTHER, active_only=True)[0]["id"]
        other_order = db.create_order(
            OTHER,
            BUYER,
            "other_buyer",
            "Other",
            [{"product_id": other_pid, "quantity": 1}],
            None,
            "Other",
            "9 Oak",
            "",
        )
        self.assertIsNotNone(other_order)
        n_prod_before = len(db.list_products(CATALOG, active_only=False))
        result = unicorn_shop.keep_only_catalog_shop()
        self.assertTrue(result["ok"])
        self.assertEqual(int(result["keeper"]), CATALOG)
        self.assertGreaterEqual(int(result["moved"]), 2)
        self.assertGreaterEqual(int(result["deactivated"]), 2)
        moved = db.get_order(int(stray["id"]))
        self.assertEqual(int(moved["chat_id"]), CATALOG)
        moved_other = db.get_order(int(other_order["id"]))
        self.assertEqual(int(moved_other["chat_id"]), CATALOG)
        # Soft-hide extras — never DELETE shops or their inventory rows.
        personal = db.get_shop(PERSONAL)
        other = db.get_shop(OTHER)
        self.assertIsNotNone(personal)
        self.assertIsNotNone(other)
        self.assertEqual(int(personal["active"] or 0), 0)
        self.assertEqual(int(other["active"] or 0), 0)
        self.assertEqual(
            len(db.list_products(CATALOG, active_only=False)), n_prod_before
        )
        self.assertEqual(len(db.list_products(PERSONAL, active_only=True)), 1)
        self.assertEqual(len(db.list_products(OTHER, active_only=True)), 1)
        catalog = unicorn_shop.find_catalog_shop()
        self.assertEqual(int(catalog["chat_id"]), CATALOG)
        codes = {o["payment_code"] for o in db.list_orders(CATALOG, limit=20)}
        self.assertIn(stray["payment_code"], codes)
        self.assertIn(self.order["payment_code"], codes)
        self.assertIn(other_order["payment_code"], codes)

    def test_keep_only_idempotent(self) -> None:
        first = unicorn_shop.keep_only_catalog_shop()
        second = unicorn_shop.keep_only_catalog_shop()
        self.assertTrue(second["ok"])
        self.assertEqual(int(second["moved"]), 0)
        self.assertEqual(int(second["deactivated"]), 0)
        self.assertEqual(int(first["keeper"]), CATALOG)
        self.assertIsNotNone(db.get_shop(PERSONAL))
        self.assertIsNotNone(db.get_shop(OTHER))

    def test_cmd_orders_lists_catalog_not_personal_shop(self) -> None:
        replies: list[str] = []

        async def reply_text(text, **_kwargs):
            replies.append(text)

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=OWNER),
            effective_chat=SimpleNamespace(id=OWNER, type="private"),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(user_data={"shop_id": PERSONAL}, bot=SimpleNamespace())
        env = {
            "PUBLIC_BOT_USERNAME": "UnicornMagicFactory2Bot",
            "PANEL_BASE_URL": "https://unicornfartzz-bot.onrender.com",
        }
        with mock.patch.object(db, "OWNER_IDS", {OWNER}), mock.patch.dict(
            os.environ, env, clear=False
        ):
            _run(bot.cmd_orders(update, context))
        self.assertTrue(replies)
        blob = replies[0]
        self.assertIn(str(self.order["id"]), blob)
        self.assertIn("Unicorn Magic Factory", blob)
        self.assertNotIn("No orders yet", blob)
        self.assertEqual(int(context.user_data["shop_id"]), CATALOG)

    def test_require_admin_catalog_for_shop_admin(self) -> None:
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=ADMIN),
            effective_chat=SimpleNamespace(id=ADMIN, type="private"),
        )
        context = SimpleNamespace(user_data={"shop_id": PERSONAL})
        env = {"PUBLIC_BOT_USERNAME": "UnicornMagicFactory2Bot"}
        with mock.patch.dict(os.environ, env, clear=False):
            sid, ok = bot._require_admin(update, context)
        self.assertTrue(ok)
        self.assertEqual(int(sid), CATALOG)


class OwnerIdsNotifyTests(unittest.TestCase):
    def test_owner_ids_includes_env_and_config(self) -> None:
        env = {
            "OWNER_TELEGRAM_CHAT_ID": "",
            "OWNER_IDS": "101,102",
            "UNICORN_NOTIFY_IDS": "103",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            config, "OWNER_IDS", {104}
        ):
            ids = vendor_stores._owner_ids()
        self.assertEqual(ids[:3], [101, 102, 103])
        self.assertIn(104, ids)

    def test_http_order_notifies_owner_ids_without_shop_admins(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db.set_db_path(Path(tmp.name) / "owner_notify.db")
        db.init_db()
        webpanel.ensure_webpanel_tables()
        db.ensure_shop(CATALOG, title="Unicorn Magic Factory")
        pid = db.add_product(CATALOG, "RETA 8MG", 80.0, stock=5)
        db.add_payment_method(CATALOG, "Venmo", "@shop-venmo memo CODE")
        key = webpanel._ensure_storefront_key(CATALOG)
        sent: list[tuple] = []

        def fake_vendor(token, chat_id, text, **kwargs):
            sent.append(("vendor", token, int(chat_id), text, kwargs))
            return True

        def fake_main(chat_id, text):
            sent.append(("main", int(chat_id), text))
            return {}

        payload = {
            "invite": key,
            "initData": build_valid_init_data(VENDOR_TOKEN, user_id=BUYER),
            "items": [{"id": pid, "vials": 1, "kits": 0}],
            "ship": {
                "name": "Buyer Bee",
                "line1": "1 Test St",
                "line2": "",
                "city": "Austin",
                "state": "TX",
                "zip": "78701",
                "phone": "512-555-0100",
            },
        }
        env = {
            "OWNER_IDS": str(OWNER),
            "OWNER_TELEGRAM_CHAT_ID": "",
            "UNICORN_NOTIFY_IDS": "",
        }
        before = spbc_notify.notify_stats()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            config, "OWNER_IDS", {OWNER}
        ), mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value=VENDOR_TOKEN
        ), mock.patch.object(
            vendor_stores,
            "get_bot_tokens_for_shop",
            return_value=[VENDOR_TOKEN],
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_vendor
        ), mock.patch.object(
            spbc_notify, "send_telegram", side_effect=fake_main
        ):
            code, body = spbc_notify.handle_http_order(payload)
        self.assertEqual(code, 200, body)
        staff = [
            s
            for s in sent
            if s[0] == "vendor" and s[2] == OWNER and "NEW ORDER" in (s[3] or "")
        ]
        self.assertTrue(staff, msg=f"OWNER_IDS staff DM missing, sent={sent!r}")
        after = spbc_notify.notify_stats()
        self.assertEqual(after["order_ok"], before["order_ok"] + 1)
        self.assertEqual(after["order_fail"], before["order_fail"])


class HealthOrdersSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "health_orders.db")
        db.init_db()
        db.ensure_shop(CATALOG, title="Unicorn Magic Factory")
        db.add_product(CATALOG, "BPC-157", 40.0, stock=3)
        db.add_admin(CATALOG, ADMIN, "ghostie", OWNER)
        pid = db.list_products(CATALOG, active_only=True)[0]["id"]
        self.order = db.create_order(
            CATALOG,
            BUYER,
            "iphone_buyer",
            "Buyer",
            [{"product_id": pid, "quantity": 1}],
            None,
            "Buyer",
            "1 St",
            "",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_health_lists_recent_orders_and_notify_counters(self) -> None:
        spbc_notify.set_keep_only_result(
            {"ok": True, "moved": 1, "stray_shops": 1}
        )
        body = spbc_notify._status_body()
        self.assertIn("notify", body)
        for key in (
            "order_ok",
            "order_fail",
            "order_empty_recipients",
            "claim_ok",
            "claim_fail",
            "claim_empty_recipients",
            "recipients_configured",
            "recipient_count",
        ):
            self.assertIn(key, body["notify"])
        self.assertTrue(body["notify"]["recipients_configured"])
        self.assertGreaterEqual(int(body["notify"]["recipient_count"]), 1)
        self.assertIn("orders", body)
        recent = body["orders"]["recent"]
        self.assertTrue(recent)
        row = recent[0]
        self.assertEqual(int(row["id"]), int(self.order["id"]))
        self.assertEqual(row["payment_code"], self.order["payment_code"])
        self.assertEqual(int(row["chat_id"]), CATALOG)
        self.assertEqual(row["status"], "pending_payment")
        self.assertEqual(row["username"], "iphone_buyer")
        self.assertEqual(body["orders"]["keep_only"]["moved"], 1)

    def test_boot_keep_only_runs_on_customer_bot(self) -> None:
        db.ensure_shop(PERSONAL, title="Shop")
        personal_pid = db.add_product(PERSONAL, "Stray Vial", 11.0, stock=2)
        stray = db.create_order(
            PERSONAL,
            BUYER,
            "iphone_buyer",
            "Buyer",
            [{"product_id": personal_pid, "quantity": 1}],
            None,
            "Buyer",
            "1 St",
            "",
        )
        self.assertIsNotNone(stray)
        env = {
            "PUBLIC_BOT_USERNAME": "UnicornMagicFactory2Bot",
            "PANEL_BASE_URL": "https://unicornfartzz-bot.onrender.com",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            run_cloud._keep_only_catalog_shop()
        moved = db.get_order(int(stray["id"]))
        self.assertEqual(int(moved["chat_id"]), CATALOG)
        health = spbc_notify._status_body()
        self.assertEqual(health["orders"]["keep_only"]["ok"], True)
        self.assertGreaterEqual(int(health["orders"]["keep_only"]["moved"]), 1)


class CustomerBotFlagTests(unittest.TestCase):
    def test_panel_host_counts_as_customer_bot(self) -> None:
        env = {
            "PUBLIC_BOT_USERNAME": "",
            "UNICORN_BOT_TOKEN": "",
            "PANEL_BASE_URL": "https://unicornfartzz-bot.onrender.com",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("UNICORN_BOT_TOKEN", None)
            os.environ["PUBLIC_BOT_USERNAME"] = ""
            self.assertTrue(unicorn_shop.is_unicorn_customer_bot())


if __name__ == "__main__":
    unittest.main()
