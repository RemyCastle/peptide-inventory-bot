"""ForceReply helper + free-text accept_prompt_message tests."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import setup_wizard as wiz  # noqa: E402
from telegram import ForceReply
from telegram.constants import ChatType


class ForceReplyHelperTests(unittest.TestCase):
    def test_bot_force_reply(self) -> None:
        fr = bot.force_reply("New product name...")
        self.assertIsInstance(fr, ForceReply)
        self.assertFalse(fr.selective)  # free-text; no forced quote-Reply
        self.assertEqual(fr.input_field_placeholder, "New product name...")

    def test_placeholder_truncated(self) -> None:
        fr = bot.force_reply("x" * 100)
        self.assertEqual(len(fr.input_field_placeholder), 64)

    def test_wizard_force_reply(self) -> None:
        fr = wiz._force_reply("Shop display name...")
        self.assertIsInstance(fr, ForceReply)
        self.assertFalse(fr.selective)
        self.assertEqual(fr.input_field_placeholder, "Shop display name...")


class AcceptPromptMessageTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, awaiting: str | None = "edit_price", age: float = 0.0) -> MagicMock:
        ctx = MagicMock()
        ctx.user_data = {}
        if awaiting is not None:
            ctx.user_data["awaiting"] = awaiting
            ctx.user_data["awaiting_at"] = time.time() - age
        ctx.bot = MagicMock()
        ctx.bot.id = 999
        return ctx

    def _update(self, chat_type: str, *, reply_to_bot: bool = False) -> MagicMock:
        msg = MagicMock()
        msg.reply_text = AsyncMock()
        if reply_to_bot:
            msg.reply_to_message = SimpleNamespace(
                from_user=SimpleNamespace(id=999)
            )
        else:
            msg.reply_to_message = None
        chat = SimpleNamespace(type=chat_type)
        upd = MagicMock()
        upd.message = msg
        upd.effective_chat = chat
        return upd

    async def test_private_accepts_without_reply(self) -> None:
        upd = self._update(ChatType.PRIVATE, reply_to_bot=False)
        ctx = self._ctx()
        ok = await bot.accept_prompt_message(upd, ctx)
        self.assertTrue(ok)
        upd.message.reply_text.assert_not_called()

    async def test_group_accepts_without_reply(self) -> None:
        """No long-press Reply required in groups either."""
        upd = self._update(ChatType.GROUP, reply_to_bot=False)
        ctx = self._ctx()
        ok = await bot.accept_prompt_message(upd, ctx)
        self.assertTrue(ok)
        upd.message.reply_text.assert_not_called()

    async def test_supergroup_accepts_without_reply(self) -> None:
        upd = self._update(ChatType.SUPERGROUP, reply_to_bot=False)
        ctx = self._ctx()
        ok = await bot.accept_prompt_message(upd, ctx)
        self.assertTrue(ok)

    async def test_expired_prompt_rejected(self) -> None:
        upd = self._update(ChatType.PRIVATE)
        ctx = self._ctx(age=bot.AWAITING_TTL_SEC + 5)
        ok = await bot.accept_prompt_message(upd, ctx)
        self.assertFalse(ok)
        upd.message.reply_text.assert_awaited()
        self.assertNotIn("awaiting", ctx.user_data)


if __name__ == "__main__":
    unittest.main()
