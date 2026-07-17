"""Kit pricing: 10-vial kits, hidden when stock < KIT_SIZE."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from config import KIT_SIZE  # noqa: E402
import bot as botmod  # noqa: E402


class KitHelpersTests(unittest.TestCase):
    def test_kit_size_default(self) -> None:
        self.assertEqual(KIT_SIZE, 10)

    def test_cart_entry_vials(self) -> None:
        self.assertEqual(db.cart_entry_vials(3), 3)
        self.assertEqual(db.cart_entry_vials({"singles": 2, "kits": 1}), 2 + KIT_SIZE)
        self.assertEqual(
            db.cart_quantity_total({1: {"singles": 1, "kits": 1}, 2: 4}),
            1 + KIT_SIZE + 4,
        )

    def test_kit_option_stock_gate(self) -> None:
        p = {"kit_price": 80.0, "stock": 15}
        self.assertTrue(db.kit_option_available(p, stock=15))
        self.assertFalse(db.kit_option_available(p, stock=9))
        self.assertFalse(db.kit_option_available({"kit_price": None, "stock": 50}, stock=50))
        self.assertFalse(db.kit_option_available({"kit_price": 0, "stock": 50}, stock=50))


class KitPricingDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "kit.db")
        db.init_db()
        self.shop = 7001
        self.admin = 5
        db.ensure_shop(self.shop, title="Kit Shop")
        db.add_admin(self.shop, self.admin, "a", self.admin)
        self.pid = db.add_product(self.shop, "Sema 5mg", 10.0, 25)
        db.add_payment_method(self.shop, "Cash App", "$x")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_and_clear_kit_price(self) -> None:
        ok, msg = db.set_product_kit_price(self.pid, self.shop, 80.0)
        self.assertTrue(ok)
        p = db.get_product(self.pid)
        self.assertEqual(db.product_kit_price(p), 80.0)
        ok2, _ = db.set_product_kit_price(self.pid, self.shop, None)
        self.assertTrue(ok2)
        self.assertIsNone(db.product_kit_price(db.get_product(self.pid)))

    def test_order_kit_charges_kit_price_deducts_ten(self) -> None:
        db.set_product_kit_price(self.pid, self.shop, 80.0)
        pay = db.list_payment_methods(self.shop)[0]
        order = db.create_order(
            self.shop,
            user_id=9,
            username="b",
            full_name="Buyer",
            items=[
                {
                    "product_id": self.pid,
                    "product_name": "Sema 5mg",
                    "unit_price": 10.0,
                    "quantity": KIT_SIZE,
                    "is_kit": True,
                }
            ],
            payment_method=pay,
            ship_name="N",
            ship_address="A",
        )
        self.assertIsNotNone(order)
        self.assertAlmostEqual(float(order["subtotal"]), 80.0, places=2)
        items = db.get_order_items(int(order["id"]))
        self.assertEqual(len(items), 1)
        self.assertEqual(int(items[0]["quantity"]), KIT_SIZE)
        self.assertIn("kit", items[0]["product_name"].lower())
        # stock not deducted until confirm
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 25)
        ok, msg, _ = db.confirm_order_payment(int(order["id"]), self.admin)
        self.assertTrue(ok, msg)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 25 - KIT_SIZE)

    def test_order_rejects_kit_when_stock_below_ten(self) -> None:
        db.set_product_kit_price(self.pid, self.shop, 80.0)
        db.update_product(self.pid, stock=9)
        pay = db.list_payment_methods(self.shop)[0]
        order = db.create_order(
            self.shop,
            user_id=9,
            username="b",
            full_name="Buyer",
            items=[
                {
                    "product_id": self.pid,
                    "product_name": "Sema 5mg",
                    "unit_price": 10.0,
                    "quantity": KIT_SIZE,
                    "is_kit": True,
                }
            ],
            payment_method=pay,
            ship_name="N",
            ship_address="A",
        )
        self.assertIsNone(order)

    def test_kit_plus_singles_subtotal(self) -> None:
        db.set_product_kit_price(self.pid, self.shop, 80.0)
        pay = db.list_payment_methods(self.shop)[0]
        order = db.create_order(
            self.shop,
            user_id=9,
            username="b",
            full_name="Buyer",
            items=[
                {
                    "product_id": self.pid,
                    "quantity": KIT_SIZE,
                    "is_kit": True,
                },
                {
                    "product_id": self.pid,
                    "quantity": 3,
                    "is_kit": False,
                },
            ],
            payment_method=pay,
            ship_name="N",
            ship_address="A",
        )
        self.assertIsNotNone(order)
        # 80 kit + 3*10 singles
        self.assertAlmostEqual(float(order["subtotal"]), 110.0, places=2)

    def test_sanitize_converts_kits_when_stock_low(self) -> None:
        db.set_product_kit_price(self.pid, self.shop, 80.0)
        p = db.get_product(self.pid)
        cart = {self.pid: {"singles": 0, "kits": 1}}
        # stock still 25 — ok
        notes = db.sanitize_cart_kits(cart, {self.pid: p})
        self.assertEqual(notes, [])
        self.assertEqual(cart[self.pid]["kits"], 1)

        db.update_product(self.pid, stock=5)
        p = db.get_product(self.pid)
        cart = {self.pid: {"singles": 0, "kits": 1}}
        notes = db.sanitize_cart_kits(cart, {self.pid: p})
        self.assertTrue(notes)
        self.assertEqual(cart[self.pid]["kits"], 0)
        # converted then capped to stock 5
        self.assertEqual(cart[self.pid]["singles"], 5)

    def test_keyboard_hides_kit_below_ten(self) -> None:
        p = {"id": 1, "name": "X", "kit_price": 80.0, "stock": 12}
        kb_ok = botmod.product_detail_keyboard(p, stock=12)
        cbs = [
            b.callback_data
            for row in kb_ok.inline_keyboard
            for b in row
            if getattr(b, "callback_data", None)
        ]
        self.assertTrue(any(c.startswith("addkit:") for c in cbs))

        kb_no = botmod.product_detail_keyboard(p, stock=9)
        cbs2 = [
            b.callback_data
            for row in kb_no.inline_keyboard
            for b in row
            if getattr(b, "callback_data", None)
        ]
        self.assertFalse(any(c.startswith("addkit:") for c in cbs2))

    def test_schema_version(self) -> None:
        self.assertGreaterEqual(db.get_schema_version(), 10)


if __name__ == "__main__":
    unittest.main()
