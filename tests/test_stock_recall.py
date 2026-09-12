"""Recall catalog stock from vault / extra Unicorn shop / stock_audit."""

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

import backup  # noqa: E402
import db  # noqa: E402
import run_cloud  # noqa: E402
import spbc_notify  # noqa: E402
import unicorn_shop  # noqa: E402

CATALOG = 41001
EXTRA = 41002
OTHER = 41003


class StockRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        db.set_db_path(self.root / "live.db")
        db.init_db()
        db.ensure_shop(CATALOG, title="Unicorn Magic Factory")
        db.ensure_shop(EXTRA, title="Unicorn Magic Factory Spare")
        db.ensure_shop(OTHER, title="Other Vendor")
        self.pid = db.add_product(CATALOG, "BPC-157 5MG", 40.0, stock=10)
        db.update_product(self.pid, sku="BPC5")
        db.add_product(CATALOG, "TB-500 10MG", 47.0, stock=10)
        db.add_product(EXTRA, "BPC-157 5MG", 40.0, stock=47)
        db.add_product(EXTRA, "TB-500 10MG", 47.0, stock=12)
        db.add_product(OTHER, "Keep Out", 9.0, stock=99)
        self._env = mock.patch.dict(
            os.environ,
            {
                "BACKUP_PASSPHRASE": "",
                "BACKUP_DIR": str(self.root / "no-vault"),
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_recall_from_generic_extra_shop(self) -> None:
        """Leftover /start shop titled Shop can still hold last-known counts."""
        stray = 41099
        db.ensure_shop(stray, title="Shop")
        db.add_product(stray, "BPC-157 5MG", 40.0, stock=33)
        # Extra Unicorn shop is all placeholder 10s so generic extra wins.
        for p in db.list_products(EXTRA, active_only=False):
            cur = int(p["stock"])
            if cur != 10:
                db.adjust_stock(p["id"], 10 - cur, reason="wipe")
        result = unicorn_shop.recall_catalog_stock()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "catalog")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 33)

    def test_recall_from_extra_unicorn_shop_not_placeholder(self) -> None:
        result = unicorn_shop.recall_catalog_stock()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "catalog")
        self.assertEqual(int(result["sku_before"]), 2)
        self.assertEqual(int(result["sku_after"]), 2)
        self.assertGreaterEqual(int(result["updated"]), 2)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 47)
        tb = next(
            p
            for p in db.list_products(CATALOG, active_only=False)
            if "TB-500" in p["name"]
        )
        self.assertEqual(int(tb["stock"]), 12)
        # Never copied the other vendor's 99.
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 47)

    def test_extra_shop_zero_does_not_clobber_live(self) -> None:
        stray = 41100
        db.ensure_shop(stray, title="Shop")
        db.add_product(stray, "BPC-157 5MG", 40.0, stock=0)
        for p in db.list_products(EXTRA, active_only=False):
            cur = int(p["stock"])
            if cur != 10:
                db.adjust_stock(p["id"], 10 - cur, reason="wipe")
        result = unicorn_shop.recall_catalog_stock()
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 10)
        self.assertEqual(int(result["updated"]), 0)

    def test_does_not_invent_placeholder_ten(self) -> None:
        db.adjust_stock(self.pid, 37, reason="shelf")  # 10 → 47
        extra_bpc = next(
            p
            for p in db.list_products(EXTRA, active_only=False)
            if "BPC" in p["name"]
        )
        db.adjust_stock(extra_bpc["id"], -37, reason="wipe")  # 47 → 10
        extra_tb = next(
            p
            for p in db.list_products(EXTRA, active_only=False)
            if "TB-500" in p["name"]
        )
        db.adjust_stock(extra_tb["id"], -2, reason="wipe")  # 12 → 10
        # Extra shop now all 10s → skip that source. Live BPC stays 47.
        result = unicorn_shop.recall_catalog_stock()
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 47)
        self.assertEqual(int(result["skipped_placeholder"]), 0)

    def test_recall_from_stock_audit(self) -> None:
        # Extra shops all-placeholder so audit is the source.
        for p in db.list_products(EXTRA, active_only=False):
            cur = int(p["stock"])
            if cur != 10:
                db.adjust_stock(p["id"], 10 - cur, reason="wipe")
        db.adjust_stock(self.pid, 25, reason="real_shelf")  # 10 → 35
        db.adjust_stock(self.pid, -25, reason="outage_reset")  # 35 → 10
        result = unicorn_shop.recall_catalog_stock()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "stock_audit")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 35)
        self.assertEqual(int(result["sku_before"]), 2)
        self.assertEqual(int(result["sku_after"]), 2)

    def test_recall_from_encrypted_vault(self) -> None:
        passphrase = "test-passphrase-not-for-prod"
        vault = self.root / "backups"
        # Snapshot with real stock, then clobber live to placeholder 10s.
        db.adjust_stock(self.pid, 37, reason="shelf")  # 47
        tb = next(
            p
            for p in db.list_products(CATALOG, active_only=False)
            if "TB-500" in p["name"]
        )
        db.adjust_stock(tb["id"], 8, reason="shelf")  # 18
        backup.create_encrypted_backup(
            db.get_db_path(), vault, passphrase, reason="pre_outage"
        )
        db.adjust_stock(self.pid, -37, reason="outage")  # back to 10
        db.adjust_stock(tb["id"], -8, reason="outage")
        env = {
            "BACKUP_PASSPHRASE": passphrase,
            "BACKUP_DIR": str(vault),
        }
        self._env.stop()
        with mock.patch.dict(os.environ, env, clear=False):
            result = unicorn_shop.recall_catalog_stock()
        self._env.start()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "vault")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 47)
        self.assertEqual(int(db.get_product(tb["id"])["stock"]), 18)
        self.assertEqual(int(result["sku_before"]), 2)
        self.assertEqual(int(result["sku_after"]), 2)
        self.assertGreaterEqual(int(result["updated"]), 2)

    def test_keep_only_then_recall_reports_health(self) -> None:
        env = {
            "PUBLIC_BOT_USERNAME": "UnicornMagicFactory2Bot",
            "PANEL_BASE_URL": "https://unicornfartzz-bot.onrender.com",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            run_cloud._keep_only_catalog_shop()
            run_cloud._recall_catalog_stock()
        extra = db.get_shop(EXTRA)
        self.assertEqual(int(extra["active"] or 0), 0)
        self.assertIsNotNone(db.get_shop(EXTRA))
        self.assertEqual(len(db.list_products(EXTRA, active_only=False)), 2)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 47)
        health = spbc_notify._status_body()
        self.assertEqual(health["orders"]["keep_only"]["ok"], True)
        self.assertGreaterEqual(int(health["orders"]["keep_only"]["deactivated"]), 2)
        self.assertEqual(health["stock_recall"]["ok"], True)
        self.assertEqual(health["stock_recall"]["sku_before"], 2)
        self.assertEqual(health["stock_recall"]["sku_after"], 2)
        self.assertEqual(health["stock_recall"]["source"], "catalog")

    def test_never_deletes_inventory_db(self) -> None:
        path = Path(db.get_db_path())
        self.assertTrue(path.is_file())
        unicorn_shop.keep_only_catalog_shop()
        unicorn_shop.recall_catalog_stock()
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(len(db.list_products(CATALOG, active_only=False)), 2)


class BackupProductReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "inventory.db"
        db.set_db_path(self.db_path)
        db.init_db()
        db.ensure_shop(CATALOG, title="Unicorn Magic Factory")
        self.pid = db.add_product(CATALOG, "Reta 35", 40.0, stock=22)
        self.passphrase = "test-passphrase-not-for-prod"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_products_does_not_replace_live_db(self) -> None:
        vault = self.root / "backups"
        path = backup.create_encrypted_backup(
            self.db_path, vault, self.passphrase, reason="snap"
        )
        db.adjust_stock(self.pid, -12, reason="live_change")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 10)
        rows = backup.read_products_from_encrypted_backup(path, self.passphrase)
        self.assertTrue(rows)
        self.assertEqual(int(rows[0]["stock"]), 22)
        # Live DB untouched by the read.
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 10)
        self.assertTrue(self.db_path.is_file())


if __name__ == "__main__":
    unittest.main()
