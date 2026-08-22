"""Website supplier notification handed off to that vendor's own bot."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import spbc_notify  # noqa: E402
import vendor_stores  # noqa: E402

SHOP = 7100
VENDOR_USER = 5150
STRANGER = 9999

PAYLOAD = {
    "order_number": "PEP-HANDOFF-1",
    "status": "paid",
    "items": [
        {"name": "DSIP 10MG", "qty": 3, "supplier": "Vendy",
         "telegram_chat_id": str(VENDOR_USER)},
    ],
    "shipping": {"name": "Jane", "line1": "1 Main St", "city": "Springfield",
                 "state": "MO", "postal": "65801"},
}


class VendorBotLookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "handoff.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Vendy Shop")
        db.add_admin(SHOP, VENDOR_USER, "vendy", VENDOR_USER)
        self._cfg = mock.patch.object(
            vendor_stores,
            "load_vendor_configs",
            lambda: [{"name": "Vendy Shop", "token": "1:TOK",
                      "shop_chat_id": SHOP, "emoji": "🛍"}],
        )
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        self._tmp.cleanup()

    def test_admin_of_vendor_shop_is_mapped(self):
        got = vendor_stores.vendor_bot_for_user(VENDOR_USER)
        self.assertIsNotNone(got)
        self.assertEqual(got["shop_chat_id"], SHOP)
        self.assertEqual(got["token"], "1:TOK")

    def test_unrelated_person_is_not_mapped(self):
        self.assertIsNone(vendor_stores.vendor_bot_for_user(STRANGER))
        self.assertIsNone(vendor_stores.vendor_bot_for_user("not-a-number"))


class HandoffDeliveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "handoff2.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Vendy Shop")
        db.add_admin(SHOP, VENDOR_USER, "vendy", VENDOR_USER)
        spbc_notify._handoffs.clear()
        spbc_notify._sessions.clear()
        spbc_notify.set_bot_token("TEST:TOKEN")
        self.vendor_sends: list[tuple] = []
        self.main_sends: list[tuple] = []

        def fake_vendor_send(token, chat_id, text, parse_mode=None, reply_markup=None):
            self.vendor_sends.append((token, int(chat_id), text, reply_markup))
            return True

        import webpanel

        self._p = [
            mock.patch.object(
                vendor_stores,
                "load_vendor_configs",
                lambda: [{"name": "Vendy Shop", "token": "1:TOK",
                          "shop_chat_id": SHOP, "emoji": "🛍"}],
            ),
            mock.patch.object(
                webpanel, "telegram_send_with_token", side_effect=fake_vendor_send
            ),
            mock.patch.object(
                spbc_notify,
                "send_telegram",
                side_effect=lambda c, t: self.main_sends.append((str(c), t)) or {},
            ),
            mock.patch.object(spbc_notify, "OWNER_TELEGRAM_CHAT_ID", "999"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        spbc_notify._handoffs.clear()
        self._tmp.cleanup()

    def test_supplier_with_vendor_bot_gets_handoff_with_buttons(self):
        code, body = spbc_notify.handle_notify(dict(PAYLOAD))
        self.assertEqual(code, 200, body)
        self.assertEqual(len(self.vendor_sends), 1)
        token, chat, text, markup = self.vendor_sends[0]
        self.assertEqual(token, "1:TOK")
        self.assertEqual(chat, VENDOR_USER)
        # Still the no-prices supplier content, with the address
        self.assertFalse(spbc_notify.leaks_prices(text), text)
        self.assertIn("1 Main St", text)
        # Actionable
        btns = markup["inline_keyboard"][0]
        self.assertTrue(btns[0]["callback_data"].startswith("shand_ok:"))
        self.assertTrue(btns[1]["callback_data"].startswith("shand_no:"))
        self.assertTrue(body["results"][0]["handoff"])
        # No duplicate plain message, and no free-text Q&A session
        self.assertEqual(self.main_sends, [])
        self.assertEqual(spbc_notify._sessions, {})

    def test_answer_is_one_shot(self):
        spbc_notify.handle_notify(dict(PAYLOAD))
        hid = next(iter(spbc_notify._handoffs))
        first = spbc_notify.set_handoff_state(hid, "accepted")
        self.assertIsNotNone(first)
        self.assertEqual(first["order_number"], "PEP-HANDOFF-1")
        self.assertIsNone(spbc_notify.set_handoff_state(hid, "declined"))

    def test_falls_back_to_main_bot_when_vendor_bot_rejects(self):
        import webpanel

        with mock.patch.object(
            webpanel, "telegram_send_with_token", return_value=False
        ):
            code, body = spbc_notify.handle_notify(dict(PAYLOAD))
        self.assertEqual(code, 200, body)
        # Order is never lost: plain supplier message went out instead
        self.assertEqual(len(self.main_sends), 1)
        self.assertEqual(self.main_sends[0][0], str(VENDOR_USER))
        self.assertEqual(spbc_notify._handoffs, {})

    def test_unmapped_supplier_keeps_the_old_flow(self):
        payload = dict(PAYLOAD)
        payload["items"] = [
            {"name": "DSIP 10MG", "qty": 3, "supplier": "Other",
             "telegram_chat_id": str(STRANGER)}
        ]
        code, body = spbc_notify.handle_notify(payload)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.vendor_sends, [])
        self.assertTrue(self.main_sends)
        self.assertIn(str(STRANGER), spbc_notify._sessions)


if __name__ == "__main__":
    unittest.main()
