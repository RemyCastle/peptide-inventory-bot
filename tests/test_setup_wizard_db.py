"""Wizard-related DB inserts: payments + setup_complete."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import payment_templates as pt  # noqa: E402


class SetupWizardDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmpdir.name) / "wiz.db")
        db.init_db()
        self.chat_id = -100555

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_setup_complete_and_payment_templates(self) -> None:
        shop = db.ensure_shop(self.chat_id, title="Group Shop")
        self.assertEqual(int(shop.get("setup_complete") or 0), 0)
        db.add_admin(self.chat_id, 7, "admin", 7)

        for payload in (
            pt.render_cashapp("$shop"),
            pt.render_venmo("@shop"),
            pt.render_crypto("ETH", "0xabc", "ERC20"),
            pt.render_zelle("a@b.com"),
            pt.render_custom("Mail a check"),
        ):
            mid = db.add_payment_from_template(self.chat_id, payload)
            self.assertGreater(mid, 0)

        methods = db.list_payment_methods(self.chat_id, active_only=True)
        self.assertEqual(len(methods), 5)
        types = {m.get("method_type") for m in methods}
        self.assertIn("cashapp", types)
        self.assertIn("crypto", types)

        db.update_shop(self.chat_id, setup_complete=1, title="Live Shop")
        shop2 = db.get_shop(self.chat_id)
        self.assertEqual(int(shop2["setup_complete"]), 1)
        self.assertEqual(shop2["title"], "Live Shop")

        # Re-run path: clear complete without duplicate shop
        db.update_shop(self.chat_id, setup_complete=0)
        shop3 = db.ensure_shop(self.chat_id, title="Live Shop")
        self.assertEqual(shop3["chat_id"], self.chat_id)


if __name__ == "__main__":
    unittest.main()
