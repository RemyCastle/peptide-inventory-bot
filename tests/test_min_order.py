"""Per-shop vial/kit minimum order rule."""

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


class MinOrderHelpersTests(unittest.TestCase):
    def test_format_plural(self) -> None:
        self.assertEqual(db.format_min_order_rule(0, "vial"), "No minimum")
        self.assertEqual(db.format_min_order_rule(1, "vial"), "1 vial minimum")
        self.assertEqual(db.format_min_order_rule(2, "vial"), "2 vials minimum")
        self.assertEqual(db.format_min_order_rule(2, "kit"), "2 kits minimum")
        self.assertEqual(db.format_min_order_rule(3, "kits"), "3 kits minimum")

    def test_cart_quantity_total(self) -> None:
        self.assertEqual(db.cart_quantity_total({1: 2, 2: 3}), 5)
        self.assertEqual(
            db.cart_quantity_total(
                [{"quantity": 1}, {"quantity": 2}, {"product_id": 9, "quantity": 4}]
            ),
            7,
        )


class MinOrderEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "minorder.db")
        db.init_db()
        self.shop = 9001
        self.admin = 11
        db.ensure_shop(self.shop, title="Min Shop")
        db.add_admin(self.shop, self.admin, "a", self.admin)
        self.p1 = db.add_product(self.shop, "Sema 5mg", 8.0, 50)
        self.p2 = db.add_product(self.shop, "BPC 10mg", 10.0, 50)
        db.add_payment_method(self.shop, "Cash App", "$test")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_off(self) -> None:
        shop = db.get_shop(self.shop)
        ok, msg = db.check_min_order(shop, 1)
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        self.assertEqual(int(db.shop_display(shop)["min_order_qty"]), 0)

    def test_set_and_block(self) -> None:
        ok, rule = db.set_min_order(self.shop, 2, label="vial")
        self.assertTrue(ok)
        self.assertIn("2", rule)
        shop = db.get_shop(self.shop)
        ok1, msg1 = db.check_min_order(shop, 1)
        self.assertFalse(ok1)
        self.assertIn("minimum", msg1.lower())
        ok2, _ = db.check_min_order(shop, 2)
        self.assertTrue(ok2)

    def test_kit_label(self) -> None:
        db.set_min_order(self.shop, 3, label="kit")
        shop = db.get_shop(self.shop)
        self.assertEqual(db.shop_display(shop)["min_order_label"], "kit")
        self.assertIn("kit", db.format_min_order_rule(3, "kit").lower())

    def test_disable(self) -> None:
        db.set_min_order(self.shop, 2, label="vial")
        ok, msg = db.set_min_order(self.shop, 0)
        self.assertTrue(ok)
        self.assertIn("disabled", msg.lower())
        shop = db.get_shop(self.shop)
        self.assertTrue(db.check_min_order(shop, 1)[0])

    def test_create_order_rejects_below_min(self) -> None:
        db.set_min_order(self.shop, 2, label="vial")
        pay = db.list_payment_methods(self.shop)[0]
        order = db.create_order(
            self.shop,
            user_id=99,
            username="buyer",
            full_name="Buyer",
            items=[
                {
                    "product_id": self.p1,
                    "product_name": "Sema 5mg",
                    "unit_price": 8.0,
                    "quantity": 1,
                }
            ],
            payment_method=pay,
            ship_name="A",
            ship_address="1 St",
        )
        self.assertIsNone(order)

        order2 = db.create_order(
            self.shop,
            user_id=99,
            username="buyer",
            full_name="Buyer",
            items=[
                {
                    "product_id": self.p1,
                    "product_name": "Sema 5mg",
                    "unit_price": 8.0,
                    "quantity": 1,
                },
                {
                    "product_id": self.p2,
                    "product_name": "BPC 10mg",
                    "unit_price": 10.0,
                    "quantity": 1,
                },
            ],
            payment_method=pay,
            ship_name="A",
            ship_address="1 St",
        )
        self.assertIsNotNone(order2)
        self.assertEqual(int(order2["id"]) > 0, True)

    def test_create_order_multi_respects_min(self) -> None:
        collab.ensure_collab_tables()
        db.set_min_order(self.shop, 2, label="kit")
        pay = db.list_payment_methods(self.shop)[0]
        bad = collab.create_order_multi(
            self.shop,
            user_id=55,
            username="u",
            full_name="U",
            items=[{"product_id": self.p1, "quantity": 1}],
            payment_method=pay,
            ship_name="N",
            ship_address="A",
        )
        self.assertIsNone(bad)
        good = collab.create_order_multi(
            self.shop,
            user_id=55,
            username="u",
            full_name="U",
            items=[{"product_id": self.p1, "quantity": 2}],
            payment_method=pay,
            ship_name="N",
            ship_address="A",
        )
        self.assertIsNotNone(good)

    def test_rejects_bad_qty(self) -> None:
        ok, msg = db.set_min_order(self.shop, -1)
        self.assertFalse(ok)
        ok2, _ = db.set_min_order(self.shop, 101)
        self.assertFalse(ok2)

    def test_schema_version(self) -> None:
        self.assertGreaterEqual(db.get_schema_version(), 9)


if __name__ == "__main__":
    unittest.main()
