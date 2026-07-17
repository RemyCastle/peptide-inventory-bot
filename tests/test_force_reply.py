"""ForceReply helper smoke tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import setup_wizard as wiz  # noqa: E402
from telegram import ForceReply


class ForceReplyHelperTests(unittest.TestCase):
    def test_bot_force_reply(self) -> None:
        fr = bot.force_reply("New product name...")
        self.assertIsInstance(fr, ForceReply)
        self.assertTrue(fr.selective)
        self.assertEqual(fr.input_field_placeholder, "New product name...")

    def test_placeholder_truncated(self) -> None:
        fr = bot.force_reply("x" * 100)
        self.assertEqual(len(fr.input_field_placeholder), 64)

    def test_wizard_force_reply(self) -> None:
        fr = wiz._force_reply("Shop display name...")
        self.assertIsInstance(fr, ForceReply)
        self.assertTrue(fr.selective)
        self.assertEqual(fr.input_field_placeholder, "Shop display name...")


if __name__ == "__main__":
    unittest.main()
