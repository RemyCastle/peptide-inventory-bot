"""Cut Unicorn Magic Factory off SPBC back-room paths. Scratch DB only."""

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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collab  # noqa: E402
import db  # noqa: E402
import order_router  # noqa: E402
import run_cloud  # noqa: E402
import site_sync  # noqa: E402
import spbc_notify  # noqa: E402
import unicorn_shop  # noqa: E402
import vendor_links  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

UNICORN = 61001
OTHER = 61002
SPBC = 61003
ADMIN = 71001
BUYER = 81001


def _run(coro):
    return asyncio.run(coro)


class UnicornIdentifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "id.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER, title="Vendy")
        db.ensure_shop(SPBC, title="SPBC Shop")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_title_and_name(self) -> None:
        self.assertTrue(unicorn_shop.is_unicorn_shop(UNICORN))
        self.assertTrue(unicorn_shop.shop_title_looks_unicorn("@unicornmagicfactory"))
        self.assertTrue(unicorn_shop.vendor_name_looks_unicorn("Unicorn Magic Factory"))
        self.assertFalse(unicorn_shop.is_unicorn_shop(OTHER))
        self.assertFalse(unicorn_shop.is_unicorn_shop(SPBC))
        self.assertFalse(unicorn_shop.shop_title_looks_unicorn("Helix Bio Labs"))

    def test_env_shop_id_without_unicorn_title(self) -> None:
        renamed = 61099
        db.ensure_shop(renamed, title="Ghostie Shop")
        self.assertFalse(unicorn_shop.is_unicorn_shop(renamed))
        with mock.patch.dict(os.environ, {"UNICORN_SHOP_CHAT_ID": str(renamed)}):
            self.assertTrue(unicorn_shop.is_unicorn_shop(renamed))
            self.assertFalse(unicorn_shop.is_unicorn_shop(OTHER))

    def test_fulfillment_gate(self) -> None:
        self.assertFalse(
            unicorn_shop.vendor_accepts_spbc_fulfillment(
                {"name": "Unicorn Magic Factory"}, UNICORN
            )
        )
        self.assertTrue(
            unicorn_shop.vendor_accepts_spbc_fulfillment(
                {"name": "Vendy"}, OTHER
            )
        )


class RouterSkipsUnicornTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "route.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER, title="Vendy")
        db.ensure_shop(SPBC, title="SPBC Shop")
        self.u_pid = db.add_product(UNICORN, "RETA 35 MG", 10.0, 50)
        db.add_product(OTHER, "RETA 35 MG", 20.0, 50)
        db.add_product(SPBC, "RETA 35 MG", 1.0, 999)
        order_router._pending.clear()
        self._cfg = mock.patch.object(order_router, "SPBC_SHOP_CHAT_ID", SPBC)
        self._cfg.start()

    def tearDown(self) -> None:
        self._cfg.stop()
        order_router._pending.clear()
        self._tmp.cleanup()

    def _payload(self):
        return {
            "order_number": "PEP-CUT-1",
            "status": "paid",
            "items": [{"name": "RETA 35 MG (Vial)", "qty": 2}],
            "total_cents": 8000,
        }

    def test_quotes_other_vendors_not_unicorn(self) -> None:
        quotes = order_router.compute_quotes(self._payload())
        ids = [q["shop_chat_id"] for q in quotes]
        self.assertEqual(ids, [OTHER])
        self.assertNotIn(UNICORN, ids)
        self.assertNotIn(SPBC, ids)

    def test_mapping_does_not_bring_unicorn_back(self) -> None:
        vendor_links.ensure_tables()
        # leftover live mapping row must not re-open quoting
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO vendor_product_links "
                "(spbc_name, shop_chat_id, vendor_product_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 't', 't')",
                ("reta 35 mg", UNICORN, self.u_pid),
            )
        self.assertIsNone(vendor_links.product_for("RETA 35 MG", UNICORN))
        quotes = order_router.compute_quotes(self._payload())
        self.assertEqual([q["shop_chat_id"] for q in quotes], [OTHER])

    def test_apply_route_refuses_unicorn_quote(self) -> None:
        stock_before = db.get_product(self.u_pid)["stock"]
        order_router._pending["dead01"] = {
            "order_number": "PEP-STALE",
            "shop_chat_id": UNICORN,
            "shop_title": "Unicorn Magic Factory",
            "total": 20.0,
            "lines": [
                {
                    "product_id": self.u_pid,
                    "name": "RETA 35 MG",
                    "deduct": 2,
                }
            ],
            "applied": False,
            "state": order_router.QUOTED,
        }
        ok, msg, _ = order_router.apply_route("dead01", actor_id=1)
        self.assertFalse(ok)
        self.assertIn("Unicorn", msg)
        self.assertEqual(db.get_product(self.u_pid)["stock"], stock_before)
        self.assertFalse(order_router._pending["dead01"]["applied"])


class CollabSkipsUnicornTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "collab.db")
        db.init_db()
        collab.ensure_collab_tables()
        db.ensure_shop(SPBC, title="SPBC Shop")
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER, title="Vendy")
        db.add_admin(SPBC, ADMIN, "remy", ADMIN)
        db.add_admin(UNICORN, ADMIN, "ghostie", ADMIN)
        db.add_admin(OTHER, ADMIN, "vendy", ADMIN)
        self.u_pid = db.add_product(UNICORN, "H36", 30.0, 20)
        self.o_pid = db.add_product(OTHER, "KPV 10MG", 12.0, 20)
        inv = collab.create_invite(SPBC, ADMIN, 10)
        ok, _ = collab.accept_invite(inv["token"], UNICORN, ADMIN)
        self.assertTrue(ok)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_share_and_guest_list_blocked(self) -> None:
        ok, msg = collab.set_share(SPBC, UNICORN, self.u_pid, 15)
        self.assertFalse(ok)
        self.assertIn("Unicorn", msg)
        self.assertEqual(collab.list_guest_products_for_host(SPBC, UNICORN), [])

    def test_leftover_share_row_hidden_from_catalog_and_orders(self) -> None:
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO shop_shares "
                "(host_chat_id, guest_chat_id, product_id, markup_pct, active, "
                "created_at, updated_at) VALUES (?, ?, ?, 15, 1, 't', 't')",
                (SPBC, UNICORN, self.u_pid),
            )
        cat = collab.catalog_for_host(SPBC)
        self.assertNotIn("H36", [x["name"] for x in cat])
        shares = collab.list_shares(SPBC, active_only=True)
        self.assertEqual(shares, [])
        order = collab.create_order_multi(
            SPBC,
            BUYER,
            "buyer",
            "Buyer",
            [{"product_id": self.u_pid, "quantity": 1}],
            None,
            "Buyer",
            "1 Main",
        )
        self.assertIsNone(order)

    def test_other_guest_still_shareable(self) -> None:
        inv = collab.create_invite(SPBC, ADMIN, 10)
        # second invite — host already has unicorn accepted; other guest
        ok, _ = collab.accept_invite(inv["token"], OTHER, ADMIN)
        self.assertTrue(ok)
        ok, msg = collab.set_share(SPBC, OTHER, self.o_pid, 10)
        self.assertTrue(ok, msg)
        names = [x["name"] for x in collab.catalog_for_host(SPBC)]
        self.assertIn("KPV 10MG", names)
        self.assertNotIn("H36", names)


class NotifyAndHandoffCutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "notify.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.add_admin(UNICORN, ADMIN, "ghostie", ADMIN)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unicorn_notify_does_not_fall_back_to_spbc_bot(self) -> None:
        calls: list[str] = []

        def fake_vendor(*_a, **_k):
            calls.append("vendor")
            return False

        def fake_main(*_a, **_k):
            calls.append("main")
            return {}

        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value="1:TOK"
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_vendor
        ), mock.patch.object(spbc_notify, "send_telegram", side_effect=fake_main):
            ok = _run(
                vendor_stores.notify_order_recipient(UNICORN, ADMIN, "NEW ORDER")
            )
        self.assertFalse(ok)
        self.assertEqual(calls, ["vendor"])

    def test_other_shop_still_falls_back_to_main(self) -> None:
        db.ensure_shop(OTHER, title="Vendy")
        calls: list[str] = []

        def fake_vendor(*_a, **_k):
            calls.append("vendor")
            return False

        def fake_main(*_a, **_k):
            calls.append("main")
            return {}

        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value="1:TOK"
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_vendor
        ), mock.patch.object(spbc_notify, "send_telegram", side_effect=fake_main):
            ok = _run(
                vendor_stores.notify_order_recipient(OTHER, ADMIN, "note")
            )
        self.assertTrue(ok)
        self.assertEqual(calls, ["vendor", "main"])

    def test_vendor_bot_for_user_skips_unicorn(self) -> None:
        with mock.patch.object(
            vendor_stores,
            "load_vendor_configs",
            lambda: [
                {
                    "name": "Unicorn Magic Factory",
                    "token": "1:UNI",
                    "shop_chat_id": UNICORN,
                    "emoji": "🦄",
                }
            ],
        ):
            self.assertIsNone(vendor_stores.vendor_bot_for_user(ADMIN))


class VendorLinksCutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "links.db")
        db.init_db()
        vendor_links.ensure_tables()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER, title="Vendy")
        self.u_pid = db.add_product(UNICORN, "H36", 30.0, 10)
        self.o_pid = db.add_product(OTHER, "H36", 31.0, 10)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cannot_map_or_list_unicorn(self) -> None:
        ok, msg = vendor_links.set_link("HGH 360IU", UNICORN, self.u_pid)
        self.assertFalse(ok)
        self.assertIn("Unicorn", msg)
        cats = vendor_links.vendor_catalogs()
        ids = {c["shop_chat_id"] for c in cats}
        self.assertNotIn(UNICORN, ids)
        self.assertIn(OTHER, ids)
        ok, _ = vendor_links.set_link("HGH 360IU", OTHER, self.o_pid)
        self.assertTrue(ok)
        self.assertIsNotNone(vendor_links.product_for("HGH 360IU", OTHER))


class SiteSyncRefusesUnicornTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "sync.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        self.pid = db.add_product(UNICORN, "Ghostie RETA", 40.0, 90)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sync_shop_does_not_fetch_or_write(self) -> None:
        fetched = []

        def boom(_base=None):
            fetched.append(1)
            return [{"name": "SEMA 10MG", "vial_price": 60}]

        with mock.patch.object(site_sync, "fetch_site_products", boom):
            with self.assertRaises(site_sync.SiteSyncError) as ctx:
                site_sync.sync_shop(chat_id=UNICORN)
        self.assertIn("Unicorn", str(ctx.exception))
        self.assertEqual(fetched, [])
        names = [p["name"] for p in db.list_products(UNICORN, active_only=False)]
        self.assertEqual(names, ["Ghostie RETA"])
        self.assertEqual(db.get_product(self.pid)["stock"], 90)


class CheckoutStillLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "co.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        self.pid = db.add_product(UNICORN, "BPC-157 5MG", 40.0, 20)
        db.add_payment_method(UNICORN, "Venmo", "@wineboos memo CODE")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_order_mints_gift_code_without_spbc(self) -> None:
        order = db.create_order(
            UNICORN,
            BUYER,
            "buyer",
            "Buyer Bee",
            [
                {
                    "product_id": self.pid,
                    "product_name": "BPC-157 5MG",
                    "unit_price": 40.0,
                    "quantity": 1,
                }
            ],
            {"id": None, "name": "Venmo"},
            "Buyer Bee",
            "1 Test St",
            "",
        )
        self.assertIsNotNone(order)
        code = str(order["payment_code"])
        self.assertTrue(code.startswith("🎁"))
        digits = code[1:]
        self.assertEqual(len(digits), 6)
        self.assertTrue(digits.isdigit())
        self.assertIn(digits[0], "23456789")
        self.assertEqual(db.get_order_by_payment_code(code)["id"], order["id"])
        self.assertEqual(int(order["chat_id"]), UNICORN)

    def test_http_order_skips_main_bot_on_unicorn(self) -> None:
        webpanel.ensure_webpanel_tables()
        sf = webpanel._ensure_storefront_key(UNICORN)
        sent: list[str] = []

        def fake_vendor(*_a, **_k):
            sent.append("vendor")
            return False

        def fake_main(*_a, **_k):
            sent.append("main")
            return {}

        token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
        user = {
            "id": BUYER,
            "first_name": "Buyer",
            "last_name": "Bee",
            "username": "buyer",
            "language_code": "en",
        }
        pairs = {
            "auth_date": str(int(time.time())),
            "query_id": "AAE_cut",
            "user": json.dumps(user, separators=(",", ":")),
        }
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        pairs["hash"] = hmac.new(
            secret, data_check.encode(), hashlib.sha256
        ).hexdigest()
        init = urllib.parse.urlencode(pairs)
        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value=token
        ), mock.patch.object(
            vendor_stores, "base_notify_ids_for_shop", return_value=[ADMIN]
        ), mock.patch.object(
            vendor_stores,
            "vendor_meta_for_shop",
            return_value={"name": "Unicorn Magic Factory", "emoji": "🦄", "notify_ids": []},
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_vendor
        ), mock.patch.object(spbc_notify, "send_telegram", side_effect=fake_main):
            code, body = spbc_notify.handle_http_order(
                {
                    "invite": sf,
                    "initData": init,
                    "items": [{"id": self.pid, "vials": 1, "kits": 0}],
                    "ship": {"name": "Buyer", "line1": "1 St"},
                }
            )
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))
        self.assertTrue(str(body.get("code") or "").startswith("🎁"))
        self.assertIn("vendor", sent)
        self.assertNotIn("main", sent)


class RunCloudVendorOnlyTests(unittest.TestCase):
    def test_no_main_token_does_not_start_spbc_bot(self) -> None:
        waited: list[bool] = []

        class FakeEvent:
            def wait(self):
                waited.append(True)

        with mock.patch("config.resolve_bot_tokens", return_value=[]), mock.patch.object(
            run_cloud.threading, "Event", FakeEvent
        ):
            run_cloud._run_foreground()
        self.assertEqual(waited, [True])


class UnicornBotHandlersTests(unittest.TestCase):
    def test_unicorn_app_omits_spbc_callback_handlers(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db.set_db_path(Path(tmp.name) / "bot.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        v = {
            "name": "Unicorn Magic Factory",
            "token": "1:TESTTOKEN",
            "emoji": "🦄",
            "store_url": "https://example.com/unicorn/",
            "notify_ids": [],
        }
        app = vendor_stores._build_app(v, UNICORN)
        patterns = []
        for group_handlers in app.handlers.values():
            for h in group_handlers:
                pat = getattr(h, "pattern", None)
                if pat is not None:
                    patterns.append(getattr(pat, "pattern", str(pat)))
        joined = " ".join(patterns)
        self.assertNotIn("voffer_", joined)
        self.assertNotIn("shand_", joined)

    def test_other_vendor_app_keeps_spbc_callback_handlers(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db.set_db_path(Path(tmp.name) / "bot2.db")
        db.init_db()
        db.ensure_shop(OTHER, title="Vendy")
        v = {
            "name": "Vendy",
            "token": "1:TESTTOKEN",
            "emoji": "🛍",
            "store_url": "https://example.com/vendy/",
            "notify_ids": [],
        }
        app = vendor_stores._build_app(v, OTHER)
        patterns = []
        for group_handlers in app.handlers.values():
            for h in group_handlers:
                pat = getattr(h, "pattern", None)
                if pat is not None:
                    patterns.append(getattr(pat, "pattern", str(pat)))
        joined = " ".join(patterns)
        self.assertIn("voffer_", joined)
        self.assertIn("shand_", joined)


if __name__ == "__main__":
    unittest.main()
