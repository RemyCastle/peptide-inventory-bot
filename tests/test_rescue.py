"""Post-ban recovery kit: fresh links, who to re-contact, readiness."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import db  # noqa: E402

SHOP_A = 5100
SHOP_B = -1001234567890  # a group shop


class RescueKitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "rescue.db")
        db.init_db()
        db.ensure_shop(SHOP_A, title="Vendor One")
        db.ensure_shop(SHOP_B, title="Group Shop")
        db.add_admin(SHOP_A, 111, "vendorone", 1)
        db.add_admin(SHOP_B, 222, None, 1)  # no username → id shown
        db.add_product(SHOP_A, "BPC", 41.0, 5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _kit(self):
        with db.get_db() as conn:
            shops = [dict(r) for r in conn.execute("SELECT * FROM shops").fetchall()]
        return bot.build_rescue_kit("newbot", shops)

    def test_kit_has_fresh_links_for_every_shop(self):
        kit = self._kit()
        self.assertIn("https://t.me/newbot?start=shop_5100", kit)
        self.assertIn(f"https://t.me/newbot?start=shop_{SHOP_B}", kit)
        self.assertIn("Vendor One", kit)
        self.assertIn("Group Shop", kit)

    def test_kit_lists_who_must_press_start(self):
        kit = self._kit()
        self.assertIn("@vendorone", kit)
        self.assertIn("id 222", kit)  # falls back to id when no username

    def test_kit_reports_product_counts(self):
        kit = self._kit()
        self.assertIn("Products      : 1", kit)

    def test_kit_flags_missing_backup_passphrase(self):
        with mock.patch.object(bot, "BACKUP_PASSPHRASE", ""):
            self.assertIn("set BACKUP_PASSPHRASE", self._kit())
        with mock.patch.object(bot, "BACKUP_PASSPHRASE", "x" * 20):
            self.assertNotIn("set BACKUP_PASSPHRASE", self._kit())

    def test_kit_flags_single_token(self):
        with mock.patch.object(bot, "resolve_bot_tokens", lambda: ["a"]):
            self.assertIn("add more with BOT_TOKENS", self._kit())
        with mock.patch.object(bot, "resolve_bot_tokens", lambda: ["a", "b"]):
            self.assertNotIn("add more with BOT_TOKENS", self._kit())

    def test_kit_includes_a_sendable_message(self):
        kit = self._kit()
        self.assertIn("Our shop bot moved to a new address", kit)
        self.assertIn("press Start", kit)

    def test_kit_handles_a_shop_with_no_admins(self):
        db.ensure_shop(5999, title="Orphan")
        kit = self._kit()
        self.assertIn("(no admins recorded)", kit)


if __name__ == "__main__":
    unittest.main()
