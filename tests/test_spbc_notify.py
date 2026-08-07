"""Supplier-notify port: grouping, message building, /notify handling, Q&A state."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import spbc_notify  # noqa: E402


PAYLOAD_PAID = {
    "order_number": "PEP-20260807-0001",
    "status": "paid",
    "items": [
        {"name": "RETA 35 MG (Vial)", "qty": 3, "supplier": "Acme", "telegram_chat_id": "111"},
        {"name": "SEMA 10MG (Kit)", "qty": 1, "supplier": "Acme"},
        {"name": "BAC WATER 3ML", "qty": 2, "supplier": "Bravo", "telegram_chat_id": "222"},
        {"name": "MYSTERY 1MG", "qty": 1},
    ],
    "shipping": {"name": "Jane", "line1": "1 Main St", "city": "Springfield", "state": "MO", "postal": "65801"},
}


class GroupingTests(unittest.TestCase):
    def test_strip_kind_suffix(self):
        self.assertEqual(spbc_notify.strip_kind_suffix("RETA 35 MG (Vial)"), "RETA 35 MG")
        self.assertEqual(spbc_notify.strip_kind_suffix("SEMA (Kit)"), "SEMA")
        self.assertEqual(spbc_notify.strip_kind_suffix("Plain"), "Plain")

    def test_groups_by_supplier_with_chat_ids(self):
        groups = spbc_notify.group_items_by_supplier(PAYLOAD_PAID)
        self.assertEqual(set(groups), {"Acme", "Bravo", "(unassigned)"})
        self.assertEqual(groups["Acme"]["chat_id"], "111")
        self.assertEqual(len(groups["Acme"]["items"]), 2)
        self.assertEqual(groups["Bravo"]["chat_id"], "222")

    def test_suppliers_meta_resolves_chat(self):
        payload = {
            "order_number": "X",
            "status": "paid",
            "items": [{"name": "KPV 10 MG", "qty": 1, "supplier": "Charlie"}],
            "suppliers": {"Charlie": {"telegram_chat_id": "333"}},
        }
        groups = spbc_notify.group_items_by_supplier(payload)
        self.assertEqual(groups["Charlie"]["chat_id"], "333")

    def test_sources_fallback_names_supplier(self):
        payload = {
            "order_number": "X",
            "status": "paid",
            "items": [{"name": "GHK-CU 34MG (Vial)", "qty": 1}],
            "sources": {"GHK-CU 34MG": "Delta · warehouse 2"},
        }
        groups = spbc_notify.group_items_by_supplier(payload)
        self.assertIn("Delta", groups)


class MessageTests(unittest.TestCase):
    def test_owner_placed_message_has_prices(self):
        text = spbc_notify.build_owner_placed_message(
            {"order_number": "PEP-1", "customer_name": "Jane", "total_cents": 15000,
             "items": [{"name": "RETA 35 MG (Vial)", "qty": 3}]}
        )
        self.assertIn("PEP-1", text)
        self.assertIn("$150.00", text)
        self.assertIn("3× RETA 35 MG (Vial)", text)

    def test_supplier_message_never_has_prices(self):
        groups = spbc_notify.group_items_by_supplier(PAYLOAD_PAID)
        for g in groups.values():
            text = spbc_notify.build_supplier_message(PAYLOAD_PAID, g)
            self.assertFalse(spbc_notify.leaks_prices(text), text)
            self.assertIn("no prices", text)

    def test_leak_guard_catches_dollar_amounts(self):
        self.assertTrue(spbc_notify.leaks_prices("Total: $450"))
        self.assertTrue(spbc_notify.leaks_prices("price 12.34 USD"))
        self.assertTrue(spbc_notify.leaks_prices("unit_price=5"))
        self.assertFalse(spbc_notify.leaks_prices("2× RETA 35 MG"))

    def test_ship_block_included(self):
        groups = spbc_notify.group_items_by_supplier(PAYLOAD_PAID)
        text = spbc_notify.build_supplier_message(PAYLOAD_PAID, groups["Acme"])
        self.assertIn("Ship to:", text)
        self.assertIn("Springfield, MO, 65801", text)


class NotifyHandlerTests(unittest.TestCase):
    def setUp(self):
        spbc_notify._sessions.clear()
        spbc_notify.set_bot_token("TEST:TOKEN")
        self.sent: list[tuple] = []

        def fake_send(chat_id, text):
            self.sent.append((str(chat_id), text))
            return {"message_id": len(self.sent)}

        self._patch = mock.patch.object(spbc_notify, "send_telegram", fake_send)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        spbc_notify._sessions.clear()

    def test_requires_order_number(self):
        code, body = spbc_notify.handle_notify({"status": "paid"})
        self.assertEqual(code, 400)

    def test_unknown_status_rejected(self):
        code, body = spbc_notify.handle_notify(
            {"order_number": "X", "status": "weird"}
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "status_not_supported")

    def test_placed_goes_to_owner(self):
        with mock.patch.object(spbc_notify, "OWNER_TELEGRAM_CHAT_ID", "999"):
            code, body = spbc_notify.handle_notify(
                {"order_number": "PEP-2", "status": "pending",
                 "items": [{"name": "A", "qty": 1}]}
            )
        self.assertEqual(code, 200)
        self.assertEqual(body["kind"], "owner_placed")
        self.assertEqual(self.sent[0][0], "999")

    def test_paid_messages_each_supplier_and_starts_follow_up(self):
        with mock.patch.object(spbc_notify, "OWNER_TELEGRAM_CHAT_ID", "999"), \
             mock.patch.object(spbc_notify, "SUPPLIER_TELEGRAM_CHAT_ID", ""):
            code, body = spbc_notify.handle_notify(dict(PAYLOAD_PAID))
        self.assertEqual(code, 200)
        # Acme + Bravo have chat ids; (unassigned) has none → 1 error
        self.assertEqual(body["messages_sent"], 2)
        self.assertEqual(body["messages_failed"], 1)
        # Each supplier got order + follow-up question
        chats = [c for c, _ in self.sent]
        self.assertEqual(chats.count("111"), 2)
        self.assertEqual(chats.count("222"), 2)
        self.assertIn("111", spbc_notify._sessions)
        self.assertEqual(spbc_notify._sessions["111"]["step"], "await_total")

    def test_follow_up_skips_owner_chat(self):
        with mock.patch.object(spbc_notify, "OWNER_TELEGRAM_CHAT_ID", "111"):
            out = spbc_notify.start_supplier_follow_up("111", "Acme", "PEP-3", [])
        self.assertFalse(out["started"])


class QnAStateTests(unittest.TestCase):
    def test_total_validation(self):
        self.assertTrue(spbc_notify._looks_like_total("450"))
        self.assertTrue(spbc_notify._looks_like_total("$450.00"))
        long_garbage = "x" * 60
        self.assertFalse(spbc_notify._looks_like_total(long_garbage))

    def test_oos_none_detection(self):
        for t in ("none", "N/A", "all good", "0"):
            self.assertTrue(spbc_notify._oos_is_none(t))
        self.assertFalse(spbc_notify._oos_is_none("RETA 35"))

    def test_owner_report_format(self):
        session = {
            "order_number": "PEP-9",
            "supplier": "Acme",
            "chat_id": "111",
            "total": "$450",
            "oos": "none",
            "items": [{"name": "RETA 35 MG", "qty": 3}],
        }
        user = SimpleNamespace(username="acme_guy", first_name="Al", last_name=None)
        text = spbc_notify.format_owner_report(session, user)
        self.assertIn("@acme_guy", text)
        self.assertIn("Total: $450", text)
        self.assertIn("Out of stock: none", text)


if __name__ == "__main__":
    unittest.main()
