"""Telegram invoice helpers: disabled by default, no Stars, no stock deduct."""

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
import tg_payments  # noqa: E402


class TgPaymentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "tgpay.db")
        db.init_db()
        db.ensure_shop(81, title="Pay Shop")
        self.pid = db.add_product(81, "Item", 20.0, stock=4)
        self.order = db.create_order(
            81,
            9,
            "b",
            "Buyer",
            [
                {
                    "product_id": self.pid,
                    "product_name": "Item",
                    "unit_price": 20.0,
                    "quantity": 1,
                }
            ],
            {"id": None, "name": "Venmo"},
            "Buyer",
            "Addr",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disabled_without_provider_token(self) -> None:
        self.assertFalse(tg_payments.invoices_enabled())
        self.assertIsNone(
            tg_payments.build_invoice_body(self.order, chat_id=9)
        )
        self.assertFalse(
            tg_payments.send_invoice_for_order(
                self.order, 81, 9, bot_token="not-a-real-bot-token"
            )
        )

    def test_rejects_stars_currency(self) -> None:
        ok, err = tg_payments.validate_pre_checkout(
            tg_payments.invoice_payload(int(self.order["id"])),
            "XTR",
            100,
        )
        self.assertFalse(ok)
        self.assertIn("Stars", err)

    def test_successful_payment_records_charge_without_deduct(self) -> None:
        payload = tg_payments.invoice_payload(int(self.order["id"]))
        ok, msg = tg_payments.apply_successful_payment(payload, "charge-abc")
        self.assertTrue(ok, msg)
        o = db.get_order(int(self.order["id"]))
        self.assertEqual(o["tg_payment_charge_id"], "charge-abc")
        self.assertEqual(o["status"], "awaiting_confirmation")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 4)

    def test_pre_checkout_amount_must_match_server_total(self) -> None:
        with mock.patch.object(tg_payments, "invoices_enabled", return_value=True):
            with mock.patch.object(
                tg_payments, "payment_provider_token", return_value="configured"
            ):
                ok, err = tg_payments.validate_pre_checkout(
                    tg_payments.invoice_payload(int(self.order["id"])),
                    "USD",
                    1,
                )
        self.assertFalse(ok)
        self.assertIn("Amount", err)

    def test_shipping_zones_null_uses_flat_fee(self) -> None:
        db.update_shop(81, shipping_fee=8.0, free_shipping_above=150.0)
        shop = db.get_shop(81)
        self.assertIsNone(db.parse_shipping_zones(shop))
        self.assertEqual(db.calc_shipping(shop, 10.0), 8.0)
        self.assertEqual(db.calc_shipping(shop, 150.0), 0.0)
        db.update_shop(
            81,
            shipping_zones=db.encode_shipping_zones(
                [{"id": "intl", "label": "Intl", "fee": 25, "free_above": 0}]
            ),
        )
        shop2 = db.get_shop(81)
        self.assertEqual(db.calc_shipping(shop2, 10.0), 8.0)
        self.assertEqual(db.calc_shipping(shop2, 10.0, zone_id="intl"), 25.0)
        self.assertEqual(db.calc_shipping(shop2, 10.0, zone_id="missing"), 8.0)


if __name__ == "__main__":
    unittest.main()
