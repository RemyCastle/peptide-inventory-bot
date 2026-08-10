"""Mini-app ship payload: store address, confirm/notify text, admin recipients."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP = 94001
ADMIN_A = 88001
ADMIN_B = 88002
OWNER = 77001
CUSTOMER = 66001


class ParseShipFieldsTests(unittest.TestCase):
    def test_full_ship_payload(self) -> None:
        name, addr, notes = vendor_stores.parse_ship_fields(
            {
                "v": 1,
                "items": [],
                "ship": {
                    "name": "  Jane Doe  ",
                    "line1": "123 Main St",
                    "line2": "Apt 4",
                    "city": "Springfield",
                    "state": "IL",
                    "zip": "62701",
                    "phone": "555-0100",
                },
            }
        )
        self.assertEqual(name, "Jane Doe")
        self.assertEqual(addr, "123 Main St\nApt 4\nSpringfield, IL 62701")
        self.assertEqual(notes, "Phone: 555-0100 · via mini app")

    def test_missing_ship_old_client(self) -> None:
        name, addr, notes = vendor_stores.parse_ship_fields({"v": 1, "items": []})
        self.assertEqual(name, "")
        self.assertEqual(addr, "")
        self.assertEqual(notes, "via mini app")

    def test_ship_not_dict(self) -> None:
        name, addr, notes = vendor_stores.parse_ship_fields({"ship": "nope"})
        self.assertEqual(name, "")
        self.assertEqual(addr, "")
        self.assertEqual(notes, "via mini app")

    def test_bad_field_types_do_not_crash(self) -> None:
        name, addr, notes = vendor_stores.parse_ship_fields(
            {
                "ship": {
                    "name": {"x": 1},
                    "line1": ["a"],
                    "line2": None,
                    "city": True,
                    "state": 12,
                    "zip": 62701,
                    "phone": None,
                }
            }
        )
        self.assertEqual(name, "")
        # state/zip coerced via str if not bool/dict/list
        self.assertIn("12", addr)
        self.assertIn("62701", addr)
        self.assertEqual(notes, "via mini app")

    def test_name_truncated_120(self) -> None:
        long = "X" * 200
        name, _, _ = vendor_stores.parse_ship_fields({"ship": {"name": long}})
        self.assertEqual(len(name), 120)

    def test_omits_empty_address_parts(self) -> None:
        _, addr, _ = vendor_stores.parse_ship_fields(
            {"ship": {"line1": "Only Street", "city": "", "state": "", "zip": ""}}
        )
        self.assertEqual(addr, "Only Street")

    def test_city_only_and_st_zip_only(self) -> None:
        _, a1, _ = vendor_stores.parse_ship_fields({"ship": {"city": "Austin"}})
        self.assertEqual(a1, "Austin")
        _, a2, _ = vendor_stores.parse_ship_fields(
            {"ship": {"state": "TX", "zip": "78701"}}
        )
        self.assertEqual(a2, "TX 78701")


class ShipMessageFormatTests(unittest.TestCase):
    def test_customer_confirm_includes_address_escaped(self) -> None:
        block = vendor_stores.format_customer_ship_block(
            "Bob_Lee", "1 *Main* St", markdown=True
        )
        self.assertIn("📦 *Shipping to:*", block)
        self.assertIn("Bob\\_Lee", block)
        self.assertIn("\\*Main\\*", block)
        self.assertIn("1 ", block)

    def test_customer_confirm_empty_when_no_ship(self) -> None:
        self.assertEqual(
            vendor_stores.format_customer_ship_block("", "", markdown=True), ""
        )

    def test_new_order_includes_address(self) -> None:
        sec = vendor_stores.format_new_order_ship_section(
            "Jane Doe", "123 Main\nSpringfield, IL 62701"
        )
        self.assertIn("Ship to:", sec)
        self.assertIn("Jane Doe", sec)
        self.assertIn("123 Main", sec)
        self.assertIn("Springfield, IL 62701", sec)

    def test_new_order_missing_address_warning(self) -> None:
        sec = vendor_stores.format_new_order_ship_section("Jane", "")
        self.assertIn("⚠️ No address provided — contact the customer", sec)
        self.assertNotIn("Ship to:", sec)


class NotifyRecipientSetTests(unittest.TestCase):
    def test_unions_shop_admins_and_dedupes(self) -> None:
        base = [OWNER, ADMIN_A]
        admins = [
            {"user_id": ADMIN_A, "chat_id": SHOP},
            {"user_id": ADMIN_B, "chat_id": SHOP},
        ]
        with mock.patch.object(db, "list_admins", return_value=admins) as la:
            got = vendor_stores.build_notify_recipient_ids(base, SHOP)
        la.assert_called_once_with(SHOP)
        self.assertEqual(got, [OWNER, ADMIN_A, ADMIN_B])

    def test_admins_only_when_base_empty(self) -> None:
        with mock.patch.object(
            db, "list_admins", return_value=[{"user_id": ADMIN_B}]
        ):
            got = vendor_stores.build_notify_recipient_ids([], SHOP)
        self.assertEqual(got, [ADMIN_B])

    def test_skips_bad_ids(self) -> None:
        with mock.patch.object(
            db,
            "list_admins",
            return_value=[{"user_id": "nope"}, {"user_id": 0}, None],
        ):
            got = vendor_stores.build_notify_recipient_ids([OWNER, "x"], SHOP)
        self.assertEqual(got, [OWNER])


class ShipOrderPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "ship_orders.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Ship Test Shop")
        self.pid = db.add_product(SHOP, "BPC-157 5MG", 40.0, stock=20)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_order_from_ship_payload_stores_fields(self) -> None:
        payload = {
            "v": 1,
            "items": [{"id": self.pid, "vials": 1, "kits": 0}],
            "ship": {
                "name": "Casey Customer",
                "line1": "9 Oak Ave",
                "line2": "",
                "city": "Denver",
                "state": "CO",
                "zip": "80202",
                "phone": "303-555-1212",
            },
        }
        ship_name, ship_address, ship_notes = vendor_stores.parse_ship_fields(payload)
        order = db.create_order(
            chat_id=SHOP,
            user_id=CUSTOMER,
            username="casey",
            full_name="Casey Customer",
            items=[{"product_id": self.pid, "quantity": 1}],
            payment_method=None,
            ship_name=ship_name,
            ship_address=ship_address,
            ship_notes=ship_notes,
        )
        self.assertIsNotNone(order)
        got = db.get_order(int(order["id"]))
        self.assertEqual(got["ship_name"], "Casey Customer")
        self.assertEqual(got["ship_address"], "9 Oak Ave\nDenver, CO 80202")
        self.assertEqual(got["ship_notes"], "Phone: 303-555-1212 · via mini app")

        # Panel public shape + history export
        webpanel.ensure_webpanel_tables()
        pub = webpanel._order_public(got)
        self.assertEqual(pub["ship_name"], "Casey Customer")
        self.assertIn("9 Oak Ave", pub["ship_address"])
        self.assertIn("Phone: 303-555-1212", pub["ship_notes"])

        text, _ = webpanel.api_order_history_txt(
            {"chat_id": SHOP, "user_id": ADMIN_A}, "2000-01-01", "2099-12-31"
        )
        self.assertIn("Ship to:", text)
        self.assertIn("Casey Customer", text)
        self.assertIn("9 Oak Ave", text)
        self.assertIn("303-555-1212", text)


class PanelShipFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "panel_ship.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Panel Ship Shop")
        webpanel.ensure_webpanel_tables()
        self.pid = db.add_product(SHOP, "TB-500", 50.0, stock=5)
        self.tok = {"chat_id": SHOP, "user_id": ADMIN_A}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_order_public_and_history_expose_ship(self) -> None:
        o = db.create_order(
            SHOP,
            CUSTOMER,
            "buyer",
            "Buyer B",
            [{"product_id": self.pid, "quantity": 1}],
            None,
            "Buyer B",
            "100 First St\nAustin, TX 78701",
            "Phone: 111 · via mini app",
        )
        self.assertIsNotNone(o)
        code, data = webpanel.api_orders(self.tok, {})
        self.assertEqual(code, 200)
        row = next(r for r in data["orders"] if r["id"] == o["id"])
        self.assertEqual(row["ship_name"], "Buyer B")
        self.assertIn("100 First St", row["ship_address"])
        self.assertIn("Phone: 111", row["ship_notes"])

        text, _ = webpanel.api_order_history_txt(self.tok, None, None)
        self.assertIn("Ship to: Buyer B · 100 First St, Austin, TX 78701 · Phone: 111 · via mini app", text)


if __name__ == "__main__":
    unittest.main()
