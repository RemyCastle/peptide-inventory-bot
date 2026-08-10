"""Dual-bot NEW ORDER notify + /resend (scratch DB only)."""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import db  # noqa: E402
import spbc_notify  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP = 95001
OWNER = 71001
VENDOR_ADMIN = 72002
CUSTOMER = 61001
VENDOR_TOKEN = "vendor-bot-token-xyz"


def _run(coro):
    return asyncio.run(coro)


class NotifyOrderRecipientTests(unittest.TestCase):
    def test_vendor_bot_success_skips_main(self) -> None:
        calls: list[str] = []

        def fake_vendor(token, chat_id, text, **kwargs):
            calls.append("vendor")
            self.assertEqual(token, VENDOR_TOKEN)
            self.assertEqual(chat_id, VENDOR_ADMIN)
            self.assertEqual(kwargs.get("parse_mode"), None)
            self.assertEqual(text, "NEW ORDER hello")
            return True

        def fake_main(chat_id, text):
            calls.append("main")
            return {}

        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value=VENDOR_TOKEN
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_vendor
        ), mock.patch.object(spbc_notify, "send_telegram", side_effect=fake_main):
            ok = _run(
                vendor_stores.notify_order_recipient(
                    SHOP, VENDOR_ADMIN, "NEW ORDER hello"
                )
            )
        self.assertTrue(ok)
        self.assertEqual(calls, ["vendor"])

    def test_vendor_fail_falls_back_to_main(self) -> None:
        calls: list[str] = []

        def fake_vendor(*_a, **_k):
            calls.append("vendor")
            return False

        def fake_main(chat_id, text):
            calls.append("main")
            self.assertEqual(chat_id, VENDOR_ADMIN)
            self.assertEqual(text, "note")
            return {}

        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value=VENDOR_TOKEN
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_vendor
        ), mock.patch.object(spbc_notify, "send_telegram", side_effect=fake_main):
            ok = _run(
                vendor_stores.notify_order_recipient(SHOP, VENDOR_ADMIN, "note")
            )
        self.assertTrue(ok)
        self.assertEqual(calls, ["vendor", "main"])

    def test_no_vendor_token_uses_main(self) -> None:
        calls: list[str] = []

        def fake_vendor(*_a, **_k):
            calls.append("vendor")
            return True

        def fake_main(chat_id, text):
            calls.append("main")
            return {}

        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value=None
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", side_effect=fake_vendor
        ), mock.patch.object(spbc_notify, "send_telegram", side_effect=fake_main):
            ok = _run(
                vendor_stores.notify_order_recipient(SHOP, VENDOR_ADMIN, "x")
            )
        self.assertTrue(ok)
        self.assertEqual(calls, ["main"])

    def test_both_fail_returns_false_no_raise(self) -> None:
        with mock.patch.object(
            vendor_stores, "get_bot_token_for_shop", return_value=VENDOR_TOKEN
        ), mock.patch.object(
            webpanel, "telegram_send_with_token", return_value=False
        ), mock.patch.object(
            spbc_notify,
            "send_telegram",
            side_effect=spbc_notify.NotifyError("boom", code="telegram_failed"),
        ):
            ok = _run(
                vendor_stores.notify_order_recipient(SHOP, VENDOR_ADMIN, "x")
            )
        self.assertFalse(ok)

    def test_bad_ids_return_false(self) -> None:
        ok = _run(
            vendor_stores.notify_order_recipient("not-int", VENDOR_ADMIN, "x")
        )
        self.assertFalse(ok)


class BuildNewOrderNotifyTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "notify_text.db")
        db.init_db()
        webpanel.ensure_webpanel_tables()
        db.ensure_shop(SHOP, title="Notify Shop")
        self.pid = db.add_product(SHOP, "BPC-157 5MG", 40.0, stock=10)
        self.order = db.create_order(
            SHOP,
            CUSTOMER,
            "buyer",
            "Buyer Name",
            [{"product_id": self.pid, "quantity": 2}],
            None,
            "Buyer Name",
            "1 Main St\nAustin, TX 78701",
            "Phone: 555 · via mini app",
        )
        self.assertIsNotNone(self.order)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_builder_includes_items_ship_and_both_links(self) -> None:
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            note = vendor_stores.build_new_order_notify_text(
                self.order, shop_name="Notify Shop", emoji="🦄"
            )
        self.assertIn("NEW ORDER", note)
        code = self.order["payment_code"]
        self.assertIn(str(code), note)
        self.assertIn("Notify Shop", note)
        self.assertIn("BPC-157 5MG", note)
        self.assertIn("Ship to:", note)
        self.assertIn("Buyer Name", note)
        self.assertIn("1 Main St", note)
        self.assertIn("✅ Confirm payment:", note)
        self.assertIn("/confirm?ct=", note)
        self.assertIn("➕ Add tracking:", note)
        self.assertIn("/track?ot=", note)
        self.assertIn("❌ Cancel order:", note)
        self.assertIn("/cancel?xt=", note)
        self.assertIn("Total:", note)


class OnWebAppDataUsesHelperTests(unittest.TestCase):
    def test_source_calls_notify_order_recipient(self) -> None:
        src = inspect.getsource(vendor_stores._build_app)
        self.assertIn("notify_order_recipient", src)
        self.assertIn("build_new_order_notify_text", src)
        # Must not use bare context.bot.send_message for owner notify loop
        self.assertNotIn("await context.bot.send_message(nid, note)", src)


class ResendCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "resend.db")
        db.init_db()
        webpanel.ensure_webpanel_tables()
        db.ensure_shop(SHOP, title="Resend Shop")
        db.add_admin(SHOP, VENDOR_ADMIN, "vendor", OWNER)
        self.pid = db.add_product(SHOP, "TB-500", 50.0, stock=5)
        self.order = db.create_order(
            SHOP,
            CUSTOMER,
            "buyer",
            "Buyer B",
            [{"product_id": self.pid, "quantity": 1}],
            None,
            "Buyer B",
            "9 Oak Ave\nDenver, CO 80202",
            "via mini app",
        )
        self.assertIsNotNone(self.order)
        self.replies: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _msg(self):
        async def reply_text(text, **_kwargs):
            self.replies.append(text)

        return SimpleNamespace(reply_text=reply_text)

    def _update(self, user_id: int):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            message=self._msg(),
        )

    def _context(self, args: list[str] | None = None):
        return SimpleNamespace(args=args or [], bot=SimpleNamespace())

    def test_resend_owner_only(self) -> None:
        with mock.patch.object(db, "is_owner", return_value=False):
            _run(bot.cmd_resend(self._update(999), self._context(["UF1"])))
        self.assertEqual(len(self.replies), 1)
        self.assertIn("Owners only", self.replies[0])

    def test_resend_unknown_order_friendly(self) -> None:
        with mock.patch.object(db, "is_owner", return_value=True):
            _run(
                bot.cmd_resend(
                    self._update(OWNER), self._context(["NO_SUCH_CODE"])
                )
            )
        self.assertEqual(len(self.replies), 1)
        self.assertIn("No order found", self.replies[0])

    def test_resend_rebuilds_notice_and_uses_helper(self) -> None:
        delivered_to: list[int] = []
        notes: list[str] = []

        async def fake_notify(shop_chat_id, recipient_id, text, context=None):
            self.assertEqual(int(shop_chat_id), SHOP)
            delivered_to.append(int(recipient_id))
            notes.append(text)
            return True

        with mock.patch.object(db, "is_owner", return_value=True), mock.patch.object(
            vendor_stores, "notify_order_recipient", side_effect=fake_notify
        ), mock.patch.object(
            vendor_stores,
            "base_notify_ids_for_shop",
            return_value=[OWNER],
        ), mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            code = self.order["payment_code"]
            _run(
                bot.cmd_resend(self._update(OWNER), self._context([str(code)]))
            )

        self.assertEqual(len(self.replies), 1)
        self.assertIn(f"Resent {code}", self.replies[0])
        self.assertIn("recipient(s)", self.replies[0])
        self.assertIn("delivered:", self.replies[0])
        # Owner base + shop admin
        self.assertIn(OWNER, delivered_to)
        self.assertIn(VENDOR_ADMIN, delivered_to)
        self.assertTrue(notes)
        note = notes[0]
        self.assertIn("NEW ORDER", note)
        self.assertIn("/confirm?ct=", note)
        self.assertIn("/track?ot=", note)
        self.assertIn("✅ Confirm payment:", note)
        self.assertIn("➕ Add tracking:", note)
        self.assertIn("TB-500", note)

    def test_resend_by_numeric_id(self) -> None:
        async def fake_notify(*_a, **_k):
            return True

        with mock.patch.object(db, "is_owner", return_value=True), mock.patch.object(
            vendor_stores, "notify_order_recipient", side_effect=fake_notify
        ), mock.patch.object(
            vendor_stores, "base_notify_ids_for_shop", return_value=[]
        ), mock.patch.object(webpanel, "PANEL_BASE_URL", "https://bot.example.com"):
            oid = int(self.order["id"])
            _run(bot.cmd_resend(self._update(OWNER), self._context([str(oid)])))
        self.assertTrue(self.replies)
        self.assertIn("Resent", self.replies[0])

    def test_shared_builder_used_by_resend_and_web_app(self) -> None:
        resend_src = inspect.getsource(bot.cmd_resend)
        app_src = inspect.getsource(vendor_stores._build_app)
        self.assertIn("build_new_order_notify_text", resend_src)
        self.assertIn("build_new_order_notify_text", app_src)
        self.assertIn("notify_order_recipient", resend_src)
        self.assertIn("CommandHandler(\"resend\"", inspect.getsource(bot.build_app))


class FindOrderForResendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "find_order.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Find Shop")
        self.pid = db.add_product(SHOP, "Item", 10.0, stock=3)
        self.order = db.create_order(
            SHOP,
            CUSTOMER,
            "u",
            "U",
            [{"product_id": self.pid, "quantity": 1}],
            None,
            "",
            "",
            "",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_by_payment_code_and_id(self) -> None:
        code = self.order["payment_code"]
        by_code = vendor_stores.find_order_for_resend(code)
        self.assertIsNotNone(by_code)
        self.assertEqual(int(by_code["id"]), int(self.order["id"]))
        by_id = vendor_stores.find_order_for_resend(str(self.order["id"]))
        self.assertIsNotNone(by_id)
        by_hash = vendor_stores.find_order_for_resend(f"#{self.order['id']}")
        self.assertIsNotNone(by_hash)
        self.assertIsNone(vendor_stores.find_order_for_resend("ZZZ999"))


if __name__ == "__main__":
    unittest.main()
