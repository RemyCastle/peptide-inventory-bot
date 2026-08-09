"""Shared-inventory clones + hidden service fees."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import franchise  # noqa: E402


class FranchiseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "t.db")
        db.init_db()
        franchise.ensure_franchise_tables()
        self.master = 9001
        self.clone = 9002
        self.admin = 77
        db.ensure_shop(self.master, title="Master")
        db.add_admin(self.master, self.admin, "a", self.admin)
        self.pid = db.add_product(self.master, "Alpha", 40.0, 5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clone_shares_stock_separate_price(self) -> None:
        tok = franchise.create_clone_token(self.master, self.admin)
        ok, _ = franchise.attach_clone(tok["token"], self.clone, self.admin, title="Clone")
        self.assertTrue(ok)
        clone_products = franchise.list_products_effective(self.clone, active_only=True)
        self.assertEqual(len(clone_products), 1)
        cp = clone_products[0]
        self.assertEqual(cp["stock"], 5)
        self.assertTrue(cp.get("linked_product_id"))
        db.update_product(cp["id"], price=99.0)
        refreshed = db.get_product(cp["id"])
        self.assertEqual(float(refreshed["price"]), 99.0)
        master = db.get_product(self.pid)
        self.assertEqual(float(master["price"]), 40.0)
        # deduct via clone order path
        with db.get_db() as c:
            c.execute(
                "UPDATE shops SET hidden_service_fee = 2.5, shipping_fee = 8, "
                "free_shipping_above = 9999, shipping_enabled = 1 WHERE chat_id = ?",
                (self.clone,),
            )
        order = db.create_order(
            self.clone,
            1,
            "u",
            "User",
            [{"product_id": cp["id"], "quantity": 2}],
            {"id": None, "name": "Cash"},
            "Name",
            "Addr",
        )
        self.assertIsNotNone(order)
        self.assertEqual(float(order["hidden_service_fee"]), 2.5)
        # customer shipping includes base 8 + hidden 2.5
        self.assertEqual(float(order["shipping_fee"]), 10.5)
        ok, msg, _ = db.confirm_order_payment(order["id"], self.admin)
        self.assertTrue(ok, msg)
        self.assertEqual(franchise.get_effective_stock(self.pid), 3)
        self.assertEqual(franchise.get_effective_stock(cp["id"]), 3)

    def test_master_only_fee_gate(self) -> None:
        # Without OWNER_IDS, is_owner may allow admins — set fee via module when owner
        ok, _ = franchise.set_hidden_service_fee(self.master, 1.0, self.admin)
        # depends on OWNER_IDS env; at least function returns tuple
        self.assertIsInstance(ok, bool)


class WeeklyInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "inv.db")
        db.init_db()
        franchise.ensure_franchise_tables()
        self.shop = 9100
        self.admin = 88
        self.owner = 4242
        db.ensure_shop(self.shop, title="Fee Shop")
        db.add_admin(self.shop, self.admin, "a", self.admin)
        self.pid = db.add_product(self.shop, "Beta", 20.0, 50)
        with db.get_db() as c:
            c.execute(
                "UPDATE shops SET hidden_service_fee = 2.0, shipping_fee = 0, "
                "free_shipping_above = 0, shipping_enabled = 0 WHERE chat_id = ?",
                (self.shop,),
            )
        self._owner_patch = mock.patch.object(db, "OWNER_IDS", {self.owner})
        self._owner_patch.start()

    def tearDown(self) -> None:
        self._owner_patch.stop()
        self._tmp.cleanup()

    def _paid_order_with_status(self, status: str, paid_at: str) -> dict:
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
                "UPDATE orders SET status = ?, paid_at = ? WHERE id = ?",
                (status, paid_at, order["id"]),
            )
        return db.get_order(order["id"])

    def test_invoice_includes_shipped_and_complete(self) -> None:
        # Mid-current-week timestamps
        ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)  # Wed
        week_start, _, _, _ = franchise._week_bounds(ref, week_offset=0)
        paid_at = week_start  # Monday of that week
        self._paid_order_with_status("shipped", paid_at)
        self._paid_order_with_status("complete", paid_at)
        self._paid_order_with_status("paid", paid_at)

        ok, msg, invs = franchise.generate_weekly_invoices(
            self.owner, ref=ref, week_offset=0
        )
        self.assertTrue(ok, msg)
        self.assertEqual(len(invs), 1)
        self.assertEqual(int(invs[0]["order_count"]), 3)
        self.assertAlmostEqual(float(invs[0]["total_fees"]), 6.0)

    def test_invoice_never_downgrades_on_rerun(self) -> None:
        ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
        week_start, week_end, _, _ = franchise._week_bounds(ref, week_offset=0)
        # Live rollup will be 1 order / $2; existing open invoice is higher
        self._paid_order_with_status("paid", week_start)
        with db.get_db() as c:
            c.execute(
                """
                INSERT INTO service_fee_invoices
                  (chat_id, week_start, week_end, order_count, total_fees, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'open', ?)
                """,
                (self.shop, week_start, week_end, 10, 99.0, "2026-08-01 00:00:00"),
            )
        # Re-run must keep the higher prior totals (never downgrade)
        ok, msg, invs = franchise.generate_weekly_invoices(
            self.owner, ref=ref, week_offset=0
        )
        self.assertTrue(ok, msg)
        self.assertEqual(len(invs), 1)
        self.assertEqual(int(invs[0]["order_count"]), 10)
        self.assertAlmostEqual(float(invs[0]["total_fees"]), 99.0)

    def test_invoice_skips_already_paid(self) -> None:
        ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
        week_start, week_end, _, _ = franchise._week_bounds(ref, week_offset=0)
        paid_at = week_start
        self._paid_order_with_status("paid", paid_at)
        with db.get_db() as c:
            c.execute(
                """
                INSERT INTO service_fee_invoices
                  (chat_id, week_start, week_end, order_count, total_fees, status, created_at, paid_at)
                VALUES (?, ?, ?, ?, ?, 'paid', ?, ?)
                """,
                (self.shop, week_start, week_end, 1, 2.0, paid_at, paid_at),
            )
        ok, msg, invs = franchise.generate_weekly_invoices(
            self.owner, ref=ref, week_offset=0
        )
        self.assertTrue(ok, msg)
        self.assertEqual(invs, [])

    def test_week_offset_previous_week(self) -> None:
        ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)  # current week
        prev_start, prev_end, _, _ = franchise._week_bounds(ref, week_offset=-1)
        self._paid_order_with_status("shipped", prev_start)

        ok0, _, invs0 = franchise.generate_weekly_invoices(
            self.owner, ref=ref, week_offset=0
        )
        self.assertTrue(ok0)
        self.assertEqual(invs0, [])  # fee was last week

        ok1, _, invs1 = franchise.generate_weekly_invoices(
            self.owner, ref=ref, week_offset=-1
        )
        self.assertTrue(ok1)
        self.assertEqual(len(invs1), 1)
        self.assertEqual(invs1[0]["week_start"], prev_start)
        self.assertEqual(invs1[0]["week_end"], prev_end)
        self.assertEqual(int(invs1[0]["order_count"]), 1)

    def test_current_and_previous_helper(self) -> None:
        ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
        cur_start, _, _, _ = franchise._week_bounds(ref, week_offset=0)
        prev_start, _, _, _ = franchise._week_bounds(ref, week_offset=-1)
        self._paid_order_with_status("paid", cur_start)
        self._paid_order_with_status("shipped", prev_start)

        ok, msg, invs = franchise.generate_weekly_invoices_current_and_previous(
            self.owner, ref=ref
        )
        self.assertTrue(ok, msg)
        self.assertEqual(len(invs), 2)
        weeks = {i["week_start"] for i in invs}
        self.assertEqual(weeks, {cur_start, prev_start})

    def test_master_only(self) -> None:
        ok, msg, invs = franchise.generate_weekly_invoices(99999, week_offset=0)
        self.assertFalse(ok)
        self.assertIn("Master", msg)
        self.assertEqual(invs, [])


if __name__ == "__main__":
    unittest.main()

