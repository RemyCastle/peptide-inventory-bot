"""Owner bridge to the spbc-orders admin API: confirm payment, set tracking."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orders_admin  # noqa: E402


class OrdersAdminTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple] = []
        self._p = [
            mock.patch.object(orders_admin, "SPBC_ORDERS_URL", "https://w.example.com"),
            mock.patch.object(orders_admin, "SPBC_ORDERS_ADMIN_TOKEN", "tok"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _fake(self, code, body):
        def _req(method, path, payload=None):
            self.calls.append((method, path, payload))
            return code, body
        return _req

    def test_mark_paid_sends_correct_patch(self):
        with mock.patch.object(
            orders_admin, "_request",
            side_effect=self._fake(200, {"order": {"total_cents": 18000}}),
        ):
            ok, msg, order = orders_admin.mark_paid("pep-1234", "by owner")
        self.assertTrue(ok, msg)
        method, path, payload = self.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(path, "/admin/orders/PEP-1234")  # upper-cased
        self.assertEqual(payload["status"], "paid")
        self.assertEqual(payload["note"], "by owner")
        self.assertEqual(order["total_cents"], 18000)

    def test_tracking_marks_shipped_and_emails(self):
        with mock.patch.object(
            orders_admin, "_request",
            side_effect=self._fake(200, {"order": {"customer_email": "a@b.c"},
                                        "email": {"sent": True}}),
        ):
            ok, msg, _ = orders_admin.set_tracking("PEP-9", "1Z999", "UPS", True)
        self.assertTrue(ok)
        self.assertIn("emailed", msg)
        _, _, payload = self.calls[0]
        self.assertEqual(payload["status"], "shipped")
        self.assertEqual(payload["tracking_number"], "1Z999")
        self.assertEqual(payload["tracking_carrier"], "UPS")
        self.assertIs(payload["send_email"], True)

    def test_tracking_can_stay_silent(self):
        with mock.patch.object(
            orders_admin, "_request", side_effect=self._fake(200, {"order": {}})
        ):
            ok, msg, _ = orders_admin.set_tracking("PEP-9", "1Z999", "", False)
        self.assertTrue(ok)
        self.assertNotIn("emailed", msg)
        _, _, payload = self.calls[0]
        self.assertIs(payload["send_email"], False)
        self.assertNotIn("tracking_carrier", payload)

    def test_errors_are_reported_not_swallowed(self):
        for code, expect in (
            (401, "token"),
            (403, "token"),
            (404, "not found"),
            (500, "Failed"),
        ):
            with mock.patch.object(
                orders_admin, "_request", side_effect=self._fake(code, {})
            ):
                ok, msg, _ = orders_admin.mark_paid("PEP-1")
            self.assertFalse(ok)
            self.assertIn(expect.lower(), msg.lower())

    def test_requires_configuration(self):
        with mock.patch.object(orders_admin, "SPBC_ORDERS_ADMIN_TOKEN", ""):
            ok, msg, _ = orders_admin.mark_paid("PEP-1")
            self.assertFalse(ok)
            self.assertIn("SPBC_ORDERS_ADMIN_TOKEN", msg)
            ok2, msg2, _ = orders_admin.set_tracking("PEP-1", "1Z")
            self.assertFalse(ok2)

    def test_validates_input(self):
        ok, _, _ = orders_admin.mark_paid("")
        self.assertFalse(ok)
        ok2, _, _ = orders_admin.set_tracking("PEP-1", "")
        self.assertFalse(ok2)


if __name__ == "__main__":
    unittest.main()
