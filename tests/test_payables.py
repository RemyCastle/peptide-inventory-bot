"""What SPBC owes vendors: recorded on accept, cleared on settle."""

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
import order_router  # noqa: E402
import payables  # noqa: E402

VENDOR_A = 9001
VENDOR_B = 9002


def quote(order="PEP-1", shop=VENDOR_A, total=135.0, order_total=180.0):
    return {
        "order_number": order,
        "shop_chat_id": shop,
        "shop_title": "Unicorn" if shop == VENDOR_A else "Other",
        "total": total,
        "order_total": order_total,
        "lines": [{"qty": 3, "name": "DSIP 10MG"}],
    }


class PayableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "pay.db")
        db.init_db()
        db.ensure_shop(VENDOR_A, title="Unicorn")
        db.ensure_shop(VENDOR_B, title="Other")
        payables.ensure_tables()

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_captures_owed_and_margin(self):
        payables.record(quote())
        totals = payables.open_totals()
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0]["owed"], 135.0)
        self.assertEqual(totals[0]["margin"], 45.0)  # 180 in, 135 out
        self.assertEqual(totals[0]["orders"], 1)

    def test_same_order_is_not_double_counted(self):
        payables.record(quote())
        payables.record(quote())  # a retry must not owe twice
        self.assertEqual(payables.open_totals()[0]["owed"], 135.0)

    def test_totals_group_by_vendor_biggest_first(self):
        payables.record(quote("PEP-1", VENDOR_A, 135.0))
        payables.record(quote("PEP-2", VENDOR_A, 65.0, 90.0))
        payables.record(quote("PEP-3", VENDOR_B, 300.0, 400.0))
        totals = payables.open_totals()
        self.assertEqual(totals[0]["shop_chat_id"], VENDOR_B)
        self.assertEqual(totals[0]["owed"], 300.0)
        self.assertEqual(totals[1]["owed"], 200.0)
        self.assertEqual(totals[1]["orders"], 2)

    def test_settle_clears_only_that_vendor(self):
        payables.record(quote("PEP-1", VENDOR_A, 135.0))
        payables.record(quote("PEP-3", VENDOR_B, 300.0, 400.0))
        n, total = payables.settle_shop(VENDOR_A, actor_id=7)
        self.assertEqual((n, total), (1, 135.0))
        remaining = payables.open_totals()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["shop_chat_id"], VENDOR_B)
        # settling twice is a no-op, not a negative
        self.assertEqual(payables.settle_shop(VENDOR_A, 7), (0, 0.0))

    def test_zero_or_bad_amounts_are_ignored(self):
        self.assertIsNone(payables.record(quote(total=0)))
        self.assertIsNone(payables.record({"order_number": "X", "total": "abc"}))
        self.assertEqual(payables.open_totals(), [])

    def test_summary_reads_plainly(self):
        self.assertIn("square", payables.summary_text())
        payables.record(quote())
        text = payables.summary_text()
        self.assertIn("Unicorn", text)
        self.assertIn("$135.00", text)
        self.assertIn("Total owed", text)


class RoutingWritesPayableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "pay2.db")
        db.init_db()
        db.ensure_shop(VENDOR_A, title="Alpha Lab")
        self.pid = db.add_product(VENDOR_A, "DSIP 10MG", 45.0, 10)
        payables.ensure_tables()
        order_router._pending.clear()
        order_router._routed.clear()
        self._cfg = mock.patch.object(order_router, "SPBC_SHOP_CHAT_ID", 0)
        self._cfg.start()
        self._env = mock.patch.dict(
            os.environ,
            {
                "SKIP_VENDOR_SHOP_CHAT_IDS": "",
                "UNICORN_SHOP_CHAT_ID": "",
                "SKIP_UNICORN_ROUTING": "1",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._cfg.stop()
        self._tmp.cleanup()

    def test_accepting_a_route_records_what_we_owe(self):
        payload = {
            "order_number": "PEP-ROUTE-1",
            "status": "paid",
            "total_cents": 18000,
            "items": [{"name": "DSIP 10MG (Vial)", "qty": 3}],
        }
        quotes = order_router.compute_quotes(payload)
        reg = order_router.register_quotes(payload, quotes)
        qid, _ = reg[0]
        ok, msg, _ = order_router.apply_route(qid, actor_id=1)
        self.assertTrue(ok, msg)
        totals = payables.open_totals()
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0]["owed"], 135.0)  # 3 × $45 her price
        self.assertEqual(totals[0]["margin"], 45.0)  # customer paid $180


if __name__ == "__main__":
    unittest.main()
