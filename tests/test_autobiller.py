"""Automatic weekly vendor billing — scratch DB only (never inventory.db)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autobiller  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import franchise  # noqa: E402


class MasterVenmoConfigTests(unittest.TestCase):
    def test_default_is_remycastle(self) -> None:
        self.assertEqual(config.MASTER_VENMO, "@remycastle")

    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"MASTER_VENMO": "@payme_please"}):
            # Re-read the same logic config uses
            val = (os.getenv("MASTER_VENMO", "") or "").strip() or "@remycastle"
            self.assertEqual(val, "@payme_please")
        with mock.patch.dict(os.environ, {"MASTER_VENMO": ""}):
            val = (os.getenv("MASTER_VENMO", "") or "").strip() or "@remycastle"
            self.assertEqual(val, "@remycastle")


class PreviousWeekBillerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "autobill.db")
        db.init_db()
        franchise.ensure_franchise_tables()
        self.shop = 9200
        self.admin = 501
        self.owner = 4242
        db.ensure_shop(self.shop, title="Vendor A")
        db.add_admin(self.shop, self.admin, "vendor_a", self.admin)
        self.pid = db.add_product(self.shop, "Gamma", 10.0, 50)
        with db.get_db() as c:
            c.execute(
                "UPDATE shops SET hidden_service_fee = 3.0, shipping_fee = 0, "
                "free_shipping_above = 0, shipping_enabled = 0 WHERE chat_id = ?",
                (self.shop,),
            )
        self._owner_patch = mock.patch.object(db, "OWNER_IDS", {self.owner})
        self._owner_patch.start()

    def tearDown(self) -> None:
        self._owner_patch.stop()
        self._tmp.cleanup()

    def _paid_order(self, paid_at: str) -> dict:
        order = db.create_order(
            self.shop,
            1,
            "u",
            "User",
            [{"product_id": self.pid, "quantity": 1}],
            {"id": None, "name": "Cash"},
            "Name",
            "Addr",
        )
        self.assertIsNotNone(order)
        ok, msg, _ = db.confirm_order_payment(order["id"], self.admin)
        self.assertTrue(ok, msg)
        with db.get_db() as c:
            c.execute(
                "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?",
                (paid_at, order["id"]),
            )
        return db.get_order(order["id"])

    def test_bills_previous_complete_week_not_current(self) -> None:
        # Wed 2026-08-05 → current week Mon 8/3–Mon 8/10; previous Mon 7/27–Mon 8/3
        ref = datetime(2026, 8, 5, 15, 0, 0, tzinfo=timezone.utc)
        cur_start, _, _, _ = franchise._week_bounds(ref, week_offset=0)
        prev_start, prev_end, _, _ = franchise._week_bounds(ref, week_offset=-1)

        self._paid_order(cur_start)  # current partial week — must NOT bill
        self._paid_order(prev_start)  # previous complete week — bill

        ok, msg, invs = franchise.bill_previous_complete_week(ref=ref)
        self.assertTrue(ok, msg)
        self.assertEqual(len(invs), 1)
        self.assertEqual(invs[0]["week_start"], prev_start)
        self.assertEqual(invs[0]["week_end"], prev_end)
        self.assertEqual(int(invs[0]["order_count"]), 1)
        self.assertAlmostEqual(float(invs[0]["total_fees"]), 3.0)

        # Current week alone still has fees but is not returned by previous-only helper
        ok0, _, invs0 = franchise.generate_weekly_invoices(
            self.owner, ref=ref, week_offset=0
        )
        self.assertTrue(ok0)
        self.assertEqual(len(invs0), 1)
        self.assertEqual(invs0[0]["week_start"], cur_start)

    def test_idempotent_second_generate(self) -> None:
        ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
        prev_start, _, _, _ = franchise._week_bounds(ref, week_offset=-1)
        self._paid_order(prev_start)
        ok1, _, invs1 = franchise.bill_previous_complete_week(ref=ref)
        ok2, _, invs2 = franchise.bill_previous_complete_week(ref=ref)
        self.assertTrue(ok1 and ok2)
        self.assertEqual(len(invs1), 1)
        self.assertEqual(len(invs2), 1)
        self.assertEqual(int(invs1[0]["id"]), int(invs2[0]["id"]))

    def test_vendor_notified_at_column_exists(self) -> None:
        with db.get_db() as c:
            cols = {r["name"] for r in c.execute(
                "PRAGMA table_info(service_fee_invoices)"
            ).fetchall()}
        self.assertIn("vendor_notified_at", cols)


class AutobillerTickTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "tick.db")
        db.init_db()
        franchise.ensure_franchise_tables()
        self.shop = 9300
        self.admin_a = 701
        self.admin_b = 702
        self.owner = 4242
        db.ensure_shop(self.shop, title="DM Shop")
        db.add_admin(self.shop, self.admin_a, "a1", self.admin_a)
        db.add_admin(self.shop, self.admin_b, "a2", self.admin_b)
        self.pid = db.add_product(self.shop, "Delta", 15.0, 20)
        with db.get_db() as c:
            c.execute(
                "UPDATE shops SET hidden_service_fee = 2.5, shipping_fee = 0, "
                "free_shipping_above = 0, shipping_enabled = 0 WHERE chat_id = ?",
                (self.shop,),
            )
        self.ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
        prev_start, _, _, _ = franchise._week_bounds(self.ref, week_offset=-1)
        order = db.create_order(
            self.shop,
            1,
            "u",
            "User",
            [{"product_id": self.pid, "quantity": 1}],
            {"id": None, "name": "Cash"},
            "Name",
            "Addr",
        )
        ok, msg, _ = db.confirm_order_payment(order["id"], self.admin_a)
        self.assertTrue(ok, msg)
        with db.get_db() as c:
            c.execute(
                "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?",
                (prev_start, order["id"]),
            )
        self.sent: list[tuple[int | str, str]] = []

        def fake_send(chat_id, text):
            self.sent.append((chat_id, text))
            return True

        self.fake_send = fake_send
        self._owner_chat = mock.patch.object(
            config, "OWNER_TELEGRAM_CHAT_ID", "999001"
        )
        self._owner_chat.start()
        # spbc_notify reads OWNER_TELEGRAM_CHAT_ID at import time into module attr
        import spbc_notify

        self._spbc_owner = mock.patch.object(
            spbc_notify, "OWNER_TELEGRAM_CHAT_ID", "999001"
        )
        self._spbc_owner.start()
        self._venmo = mock.patch.object(config, "MASTER_VENMO", "@remycastle")
        self._venmo.start()

    def tearDown(self) -> None:
        self._venmo.stop()
        self._spbc_owner.stop()
        self._owner_chat.stop()
        self._tmp.cleanup()

    def test_vendor_dm_once_and_stamp(self) -> None:
        r1 = autobiller.run_billing_tick(
            ref=self.ref, send_fn=self.fake_send, require_token=False
        )
        self.assertTrue(r1["ok"], r1.get("error"))
        self.assertEqual(len(r1["notified"]), 1)
        inv_id = int(r1["notified"][0]["id"])

        # Both shop admins get the invoice DM
        vendor_msgs = [
            (cid, t)
            for cid, t in self.sent
            if cid in (self.admin_a, self.admin_b)
        ]
        self.assertEqual(len(vendor_msgs), 2)
        for cid, text in vendor_msgs:
            self.assertIn("You owe: $2.50", text)
            self.assertIn("@remycastle", text)
            self.assertIn("Weekly invoice", text)
            self.assertIn("DM Shop", text)

        with db.get_db() as c:
            row = c.execute(
                "SELECT vendor_notified_at FROM service_fee_invoices WHERE id = ?",
                (inv_id,),
            ).fetchone()
        self.assertIsNotNone(row["vendor_notified_at"])

        n_after_first = len(self.sent)
        r2 = autobiller.run_billing_tick(
            ref=self.ref, send_fn=self.fake_send, require_token=False
        )
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["notified"], [])
        # No additional vendor DMs (owner summary may still fire if invoices returned)
        vendor_after = [
            (cid, t)
            for cid, t in self.sent[n_after_first:]
            if cid in (self.admin_a, self.admin_b)
        ]
        self.assertEqual(vendor_after, [])

    def test_dm_targets_admins_and_contains_amount_and_venmo(self) -> None:
        with mock.patch.object(config, "MASTER_VENMO", "@custom_venmo"):
            r = autobiller.run_billing_tick(
                ref=self.ref, send_fn=self.fake_send, require_token=False
            )
        self.assertTrue(r["ok"])
        admin_ids = {self.admin_a, self.admin_b}
        targets = {cid for cid, _ in self.sent if cid in admin_ids}
        self.assertEqual(targets, admin_ids)
        sample = next(t for cid, t in self.sent if cid == self.admin_a)
        self.assertIn("$2.50", sample)
        self.assertIn("@custom_venmo", sample)

    def test_owner_summary_produced(self) -> None:
        r = autobiller.run_billing_tick(
            ref=self.ref, send_fn=self.fake_send, require_token=False
        )
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["owner_summary"])
        self.assertIn("Total billed", r["owner_summary"])
        self.assertIn("DM Shop", r["owner_summary"])
        owner_msgs = [t for cid, t in self.sent if str(cid) == "999001"]
        self.assertEqual(len(owner_msgs), 1)
        self.assertIn("$2.50", owner_msgs[0])

    def test_send_failure_does_not_stamp(self) -> None:
        def fail_send(chat_id, text):
            self.sent.append((chat_id, text))
            return False

        r = autobiller.run_billing_tick(
            ref=self.ref, send_fn=fail_send, require_token=False
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["notified"], [])
        self.assertTrue(r["failed_notify"])
        pending = franchise.list_unnotified_open_invoices()
        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0].get("vendor_notified_at"))

    def test_tick_exception_is_caught(self) -> None:
        with mock.patch.object(
            franchise,
            "bill_previous_complete_week",
            side_effect=RuntimeError("boom"),
        ):
            r = autobiller.run_billing_tick(
                ref=self.ref, send_fn=self.fake_send, require_token=False
            )
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"] or "")

    def test_format_vendor_message_default_venmo(self) -> None:
        inv = {
            "week_start": "2026-07-27 00:00:00",
            "week_end": "2026-08-03 00:00:00",
            "title": "Shop X",
            "order_count": 4,
            "total_fees": 12.0,
        }
        text = franchise.format_vendor_invoice_dm(inv, master_venmo="@remycastle")
        self.assertIn("Jul 27–Aug 02", text)
        self.assertIn("Orders: 4", text)
        self.assertIn("You owe: $12.00", text)
        self.assertIn("Pay via Venmo: @remycastle", text)


if __name__ == "__main__":
    unittest.main()
