"""Unit tests for payment template rendering."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import payment_templates as pt  # noqa: E402
import permissions  # noqa: E402


class PaymentTemplateTests(unittest.TestCase):
    def test_cashapp(self) -> None:
        p = pt.render_cashapp("mycash")
        self.assertEqual(p["method_type"], "cashapp")
        self.assertEqual(p["cashtag"], "$mycash")
        self.assertIn("$mycash", p["instructions"])
        self.assertEqual(p["name"], "Cash App")

    def test_venmo(self) -> None:
        p = pt.render_venmo("seller")
        self.assertEqual(p["handle"], "@seller")
        self.assertIn("@seller", p["instructions"])

    def test_crypto(self) -> None:
        p = pt.render_crypto("USDT", "TXabc123", "TRC20 only")
        self.assertEqual(p["method_type"], "crypto")
        self.assertIn("TXabc123", p["instructions"])
        self.assertIn("TRC20", p["instructions"])
        self.assertIn("network", p["instructions"].lower())

    def test_zelle(self) -> None:
        p = pt.render_zelle("pay@example.com")
        self.assertEqual(p["method_type"], "zelle")
        self.assertIn("pay@example.com", p["instructions"])

    def test_custom(self) -> None:
        p = pt.render_custom("Wire to bank XYZ", name="Wire")
        self.assertEqual(p["name"], "Wire")
        self.assertIn("Wire to bank", p["instructions"])

    def test_render_from_answers_crypto(self) -> None:
        p = pt.render_from_answers("crypto", ["BTC", "bc1qtest", "-"])
        self.assertIn("bc1qtest", p["instructions"])
        self.assertEqual(p["chain"], "BTC")


class GroupAdminPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_group_admin_true_for_administrator(self) -> None:
        bot = MagicMock()
        member = MagicMock()
        member.status = "administrator"
        bot.get_chat_member = AsyncMock(return_value=member)
        ok = await permissions.is_group_admin(bot, -100, 1)
        self.assertTrue(ok)

    async def test_is_group_admin_false_for_member(self) -> None:
        bot = MagicMock()
        member = MagicMock()
        member.status = "member"
        bot.get_chat_member = AsyncMock(return_value=member)
        ok = await permissions.is_group_admin(bot, -100, 1)
        self.assertFalse(ok)

    async def test_is_group_admin_false_on_api_error(self) -> None:
        from telegram.error import TelegramError

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=TelegramError("nope"))
        ok = await permissions.is_group_admin(bot, -100, 1)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
