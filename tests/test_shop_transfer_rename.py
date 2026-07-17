"""Shop rename + transfer-to-group (chat_id remap + deep-link aliases)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collab  # noqa: E402
import db  # noqa: E402
import franchise  # noqa: E402


class RenameShopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "rename.db")
        db.init_db()
        self.shop = 1001
        self.admin = 42
        self.stranger = 99
        db.ensure_shop(self.shop, title="Old Name")
        db.add_admin(self.shop, self.admin, "boss", self.admin)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rename_ok(self) -> None:
        ok, title = db.rename_shop(self.shop, "  New Shop  ", by_user=self.admin)
        self.assertTrue(ok)
        self.assertEqual(title, "New Shop")
        self.assertEqual(db.get_shop(self.shop)["title"], "New Shop")

    def test_rename_admin_only(self) -> None:
        ok, msg = db.rename_shop(self.shop, "Nope", by_user=self.stranger)
        self.assertFalse(ok)
        self.assertIn("Admin", msg)

    def test_rename_rejects_empty(self) -> None:
        ok, msg = db.rename_shop(self.shop, "   ", by_user=self.admin)
        self.assertFalse(ok)
        self.assertIn("empty", msg.lower())

    def test_rename_rejects_too_long(self) -> None:
        ok, msg = db.rename_shop(self.shop, "x" * 100, by_user=self.admin)
        self.assertFalse(ok)
        self.assertIn("long", msg.lower())


class TransferShopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "xfer.db")
        db.init_db()
        collab.ensure_collab_tables()
        franchise.ensure_franchise_tables()
        self.source = -5001  # group-style id
        self.target = -5002
        self.other = -5003
        self.admin = 77
        self.stranger = 88
        db.ensure_shop(self.source, title="Source Shop")
        db.add_admin(self.source, self.admin, "a", self.admin)
        self.pid = db.add_product(
            self.source, "Tren Ace", 45.0, 10, description="test"
        )
        db.add_payment_method(self.source, "Cash App", "$shop")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_transfer_moves_catalog_and_aliases(self) -> None:
        tok = db.create_transfer_token(self.source, self.admin)
        ok, msg, new_id = db.transfer_shop_to_group(
            tok["token"], self.target, self.admin, title="Dest Group"
        )
        self.assertTrue(ok, msg)
        self.assertEqual(new_id, self.target)

        # Source row gone; target has shop + products
        self.assertIsNone(db.get_shop(self.source))
        shop = db.get_shop(self.target)
        self.assertIsNotNone(shop)
        self.assertEqual(shop["title"], "Dest Group")

        products = db.list_products(self.target)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name"], "Tren Ace")
        self.assertEqual(int(products[0]["stock"]), 10)

        pays = db.list_payment_methods(self.target)
        self.assertEqual(len(pays), 1)

        # Admin preserved
        self.assertTrue(db.is_admin(self.target, self.admin))

        # Deep-link alias
        self.assertEqual(db.resolve_shop_chat_id(self.source), self.target)
        resolved = db.get_shop_resolved(self.source)
        self.assertIsNotNone(resolved)
        self.assertEqual(int(resolved["chat_id"]), self.target)

        # Token consumed
        inv = db.get_transfer_token(tok["token"])
        self.assertEqual(inv["status"], "used")

    def test_transfer_refuses_non_empty_target(self) -> None:
        db.ensure_shop(self.target, title="Busy")
        db.add_product(self.target, "Other", 1.0, 1)
        tok = db.create_transfer_token(self.source, self.admin)
        ok, msg, _ = db.transfer_shop_to_group(
            tok["token"], self.target, self.admin
        )
        self.assertFalse(ok)
        self.assertIn("already", msg.lower())
        # Source intact
        self.assertIsNotNone(db.get_shop(self.source))
        self.assertEqual(len(db.list_products(self.source)), 1)

    def test_transfer_replaces_empty_placeholder_target(self) -> None:
        db.ensure_shop(self.target, title="Empty placeholder")
        tok = db.create_transfer_token(self.source, self.admin)
        ok, msg, new_id = db.transfer_shop_to_group(
            tok["token"], self.target, self.admin, title="Live"
        )
        self.assertTrue(ok, msg)
        self.assertEqual(new_id, self.target)
        self.assertEqual(len(db.list_products(self.target)), 1)

    def test_transfer_admin_only(self) -> None:
        tok = db.create_transfer_token(self.source, self.admin)
        ok, msg, _ = db.transfer_shop_to_group(
            tok["token"], self.target, self.stranger
        )
        self.assertFalse(ok)
        self.assertIn("admin", msg.lower())

    def test_transfer_updates_master_pointers(self) -> None:
        # Clone-style pointer from other shop to source
        db.ensure_shop(self.other, title="Clone")
        with db.get_db() as conn:
            conn.execute(
                "UPDATE shops SET inventory_master_chat_id = ?, clone_of_chat_id = ? "
                "WHERE chat_id = ?",
                (self.source, self.source, self.other),
            )
        tok = db.create_transfer_token(self.source, self.admin)
        ok, msg, _ = db.transfer_shop_to_group(
            tok["token"], self.target, self.admin
        )
        self.assertTrue(ok, msg)
        other = db.get_shop(self.other)
        self.assertEqual(int(other["inventory_master_chat_id"]), self.target)
        self.assertEqual(int(other["clone_of_chat_id"]), self.target)

    def test_transfer_updates_collab_host(self) -> None:
        guest = -6000
        db.ensure_shop(guest, title="Guest")
        db.add_admin(guest, self.admin, "a", self.admin)
        gpid = db.add_product(guest, "Guest SKU", 20.0, 3)
        inv = collab.create_invite(self.source, self.admin, default_markup_pct=10)
        ok, _ = collab.accept_invite(inv["token"], guest, self.admin)
        self.assertTrue(ok)
        collab.set_share(self.source, guest, gpid, markup_pct=10)

        tok = db.create_transfer_token(self.source, self.admin)
        ok, msg, _ = db.transfer_shop_to_group(
            tok["token"], self.target, self.admin
        )
        self.assertTrue(ok, msg)

        shares = collab.list_shares(self.target, active_only=True)
        self.assertEqual(len(shares), 1)
        self.assertEqual(int(shares[0]["host_chat_id"]), self.target)
        self.assertEqual(int(shares[0]["guest_chat_id"]), guest)

    def test_second_token_cancels_first(self) -> None:
        t1 = db.create_transfer_token(self.source, self.admin)
        t2 = db.create_transfer_token(self.source, self.admin)
        inv1 = db.get_transfer_token(t1["token"])
        inv2 = db.get_transfer_token(t2["token"])
        self.assertEqual(inv1["status"], "cancelled")
        self.assertEqual(inv2["status"], "pending")


if __name__ == "__main__":
    unittest.main()
