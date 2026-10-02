"""ADMIN_TELEGRAM_IDS is required for admin commands. Empty denies everyone."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot
import config
import db
import permissions


def _run(coro):
    return asyncio.run(coro)


class AllowlistTests(unittest.TestCase):
    def test_empty_allowlist_denies(self) -> None:
        with mock.patch.object(config, "ADMIN_TELEGRAM_IDS", set()):
            self.assertFalse(permissions.is_allowlisted_admin(4242))
            self.assertFalse(permissions.is_allowlisted_admin(None))
            self.assertFalse(permissions.is_allowlisted_admin("nope"))

    def test_only_listed_ids_pass(self) -> None:
        with mock.patch.object(config, "ADMIN_TELEGRAM_IDS", {4242, 7}):
            self.assertTrue(permissions.is_allowlisted_admin(4242))
            self.assertTrue(permissions.is_allowlisted_admin("7"))
            self.assertFalse(permissions.is_allowlisted_admin(8))

    def test_owner_bit_is_not_enough(self) -> None:
        with mock.patch.object(config, "ADMIN_TELEGRAM_IDS", set()), mock.patch.object(
            db, "is_owner", return_value=True
        ):
            self.assertFalse(bot._staff_owner(4242))

    def test_listed_owner_passes(self) -> None:
        with mock.patch.object(config, "ADMIN_TELEGRAM_IDS", {4242}), mock.patch.object(
            db, "is_owner", return_value=True
        ):
            self.assertTrue(bot._staff_owner(4242))

    def test_resend_denied_when_unlisted(self) -> None:
        replies: list[str] = []

        async def reply_text(text, **_kwargs):
            replies.append(text)

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=4242),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(args=["UF1"], bot=SimpleNamespace())
        with mock.patch.object(config, "ADMIN_TELEGRAM_IDS", set()), mock.patch.object(
            db, "is_owner", return_value=True
        ):
            _run(bot.cmd_resend(update, context))
        self.assertEqual(replies, ["Owners only."])


if __name__ == "__main__":
    unittest.main()
