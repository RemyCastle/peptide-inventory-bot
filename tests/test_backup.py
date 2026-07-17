"""Encrypted backup / restore / prune."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backup  # noqa: E402
import db  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "inventory.db"
        db.set_db_path(self.db_path)
        db.init_db()
        self.shop = 7001
        db.ensure_shop(self.shop, title="Backup Shop")
        db.add_product(self.shop, name="Reta 35", price=40.0, stock=12)
        self.passphrase = "test-passphrase-not-for-prod"
        self.vault = self.root / "backups"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip_restores_stock(self) -> None:
        path = backup.create_encrypted_backup(
            self.db_path,
            self.vault,
            self.passphrase,
            reason="paid_confirm",
        )
        self.assertTrue(path.is_file())
        latest = self.vault / "latest.enc"
        self.assertTrue(latest.is_file())

        # Mutate live DB
        prods = db.list_products(self.shop)
        self.assertEqual(prods[0]["stock"], 12)
        db.adjust_stock(prods[0]["id"], -5, actor_id=1, reason="test")
        self.assertEqual(db.get_product(prods[0]["id"])["stock"], 7)

        # Restore snapshot
        other_db = self.root / "restored.db"
        meta = backup.restore_encrypted_backup(
            latest, other_db, self.passphrase, backup_existing=False
        )
        self.assertIn("created_at", meta)

        db.set_db_path(other_db)
        prods2 = db.list_products(self.shop)
        self.assertEqual(len(prods2), 1)
        self.assertEqual(prods2[0]["stock"], 12)
        self.assertEqual(prods2[0]["name"], "Reta 35")

    def test_bad_passphrase_fails(self) -> None:
        path = backup.create_encrypted_backup(
            self.db_path, self.vault, self.passphrase, reason="manual"
        )
        with self.assertRaises(Exception):
            backup.restore_encrypted_backup(
                path, self.root / "x.db", "wrong-pass", backup_existing=False
            )

    def test_prune_keeps_latest(self) -> None:
        backup.create_encrypted_backup(
            self.db_path, self.vault, self.passphrase, reason="a"
        )
        latest = self.vault / "latest.enc"
        self.assertTrue(latest.is_file())
        # Create a fake old file
        old = self.vault / "inventory-old.enc"
        old.write_bytes(latest.read_bytes())
        import os
        import time

        old_time = time.time() - (40 * 86400)
        os.utime(old, (old_time, old_time))
        removed = backup.prune_old_backups(self.vault, retention_days=30)
        self.assertGreaterEqual(removed, 1)
        self.assertTrue(latest.is_file())
        self.assertFalse(old.exists())


class TokenPoolTests(unittest.TestCase):
    def test_parse_and_failover_index(self) -> None:
        import token_pool

        tokens = token_pool.parse_tokens("aaa,bbb,ccc", "aaa")
        self.assertEqual(tokens, ["aaa", "bbb", "ccc"])
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            token_pool.save_state(state, {"active_index": 0, "dead_tokens": []})
            nxt = token_pool.mark_token_dead(state, "aaa", tokens, 0)
            self.assertEqual(nxt, 1)
            st = token_pool.load_state(state)
            self.assertEqual(st["active_index"], 1)

    def test_fatal_errors(self) -> None:
        import token_pool

        class InvalidToken(Exception):
            pass

        self.assertTrue(token_pool.is_fatal_token_error(InvalidToken("bad")))
        self.assertTrue(
            token_pool.is_fatal_token_error(RuntimeError("Unauthorized"))
        )
        self.assertFalse(
            token_pool.is_fatal_token_error(RuntimeError("Conflict: terminated"))
        )


if __name__ == "__main__":
    unittest.main()
