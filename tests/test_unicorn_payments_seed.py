"""Unicorn payment-method seed + empty-rails guards (scratch DB only)."""

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

import db  # noqa: E402
import payment_templates  # noqa: E402
import run_cloud  # noqa: E402
import spbc_notify  # noqa: E402
import unicorn_shop  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

UNICORN = 82001
OTHER = 82002
PAGES_KEY = unicorn_shop.PAGES_STOREFRONT_KEY


class UnicornPaymentSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "pay_seed.db")
        db.init_db()
        db.ensure_shop(UNICORN, title="Unicorn Magic Factory")
        db.ensure_shop(OTHER, title="Other Vendor")
        db.add_product(UNICORN, "BPC-157 5MG", 40.0, stock=6)
        db.add_product(OTHER, "Other Vial", 9.0, stock=2)
        webpanel.ensure_webpanel_tables()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pages_bind_seeds_without_claim_token(self) -> None:
        self.assertEqual(db.list_payment_methods(UNICORN, active_only=False), [])
        env = {"UNICORN_CLAIM_TOKEN": "", "UNICORN_SHOP_CHAT_ID": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("UNICORN_CLAIM_TOKEN", None)
            os.environ.pop("UNICORN_SHOP_CHAT_ID", None)
            os.environ.pop("UNICORN_STOREFRONT_KEY", None)
            run_cloud._bind_unicorn_pages_storefront()
        methods = db.list_payment_methods(UNICORN, active_only=True)
        types = {m["method_type"] for m in methods}
        self.assertEqual(types, {"venmo", "paypal"})
        self.assertTrue(any("@wineboos" in (m.get("handle") or "") for m in methods))
        # Other shops are not seeded
        self.assertEqual(db.list_payment_methods(OTHER, active_only=False), [])

    def test_seed_is_idempotent_and_leaves_paused_alone(self) -> None:
        first = webpanel.ensure_unicorn_shop_payments(UNICORN)
        self.assertEqual(sorted(first["created"]), ["paypal", "venmo"])
        venmo = next(
            m
            for m in db.list_payment_methods(UNICORN, active_only=False)
            if m["method_type"] == "venmo"
        )
        db.update_payment_method(venmo["id"], handle="@edited", active=0)
        second = webpanel.ensure_unicorn_shop_payments(UNICORN)
        self.assertEqual(second["created"], [])
        again = db.get_payment_method(venmo["id"])
        self.assertEqual(again["handle"], "@edited")
        self.assertEqual(int(again["active"]), 0)

    def test_health_exposes_payment_counts_no_handles(self) -> None:
        webpanel.ensure_unicorn_shop_payments(UNICORN)
        body = spbc_notify._status_body()
        self.assertEqual(body["payments"]["active"], 2)
        self.assertEqual(body["payments"]["total"], 2)
        blob = str(body["payments"])
        self.assertNotIn("wineboos", blob)
        self.assertNotIn("proton", blob)
        self.assertEqual(body["invoices"]["enabled"], False)
        self.assertNotIn("LIVE", str(body.get("invoices")))

    def test_panel_seed_defaults_unicorn_only(self) -> None:
        uni_tok = {"chat_id": UNICORN, "user_id": 1}
        other_tok = {"chat_id": OTHER, "user_id": 1}
        code, data = webpanel.api_payment(other_tok, {"seed_defaults": True})
        self.assertEqual(code, 400, data)
        self.assertIn("Unicorn", data.get("error") or "")
        self.assertEqual(db.list_payment_methods(OTHER, active_only=False), [])
        code, data = webpanel.api_payment(uni_tok, {"seed_defaults": True})
        self.assertEqual(code, 200, data)
        self.assertTrue(data.get("ok"))
        self.assertIn("venmo", data.get("created") or [])
        state_code, state = webpanel.api_state(uni_tok)
        self.assertEqual(state_code, 200)
        self.assertTrue(state["shop"]["is_unicorn"])
        other_code, other_state = webpanel.api_state(other_tok)
        self.assertEqual(other_code, 200)
        self.assertFalse(other_state["shop"]["is_unicorn"])

    def test_storefront_payment_methods_name_and_type_only(self) -> None:
        webpanel.ensure_unicorn_shop_payments(UNICORN)
        webpanel.ensure_storefront_key_plain(UNICORN, PAGES_KEY)
        code, body = webpanel.api_storefront(PAGES_KEY)
        self.assertEqual(code, 200, body)
        self.assertIn("Venmo", body["payments"])
        self.assertIn("PayPal", body["payments"])
        kinds = {m["method_type"] for m in body["payment_methods"]}
        self.assertEqual(kinds, {"venmo", "paypal"})
        for m in body["payment_methods"]:
            self.assertNotIn("handle", m)
            self.assertNotIn("instructions", m)
            self.assertNotIn("target", m)

    def test_empty_buyer_copy_does_not_promise_a_dm(self) -> None:
        text = vendor_stores.build_customer_order_received_text(
            {"id": 1, "total": 10, "payment_code": "ABC"},
            UNICORN,
            markdown=False,
            order_lines=[],
        )
        self.assertIn("No payment method is published yet", text)
        self.assertNotIn("DM'd", text)
        html = vendor_stores.build_customer_order_received_html(
            {"id": 1, "total": 10, "payment_code": "ABC"},
            UNICORN,
            order_lines=[],
        )
        self.assertIn("No payment method is published yet", html)

    def test_bot_registers_seed_and_paypal_quick_add(self) -> None:
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('callback_data="adm_seedpays"', src)
        self.assertIn('callback_data="paytpl:paypal"', src)
        self.assertIn('callback_data="paytpl:apple_cash"', src)
        self.assertIn("cb_adm_seedpays", src)

    def test_pay_url_venmo_prefill_paypal_username_not_email(self) -> None:
        venmo = payment_templates.render_venmo("@wineboos")
        link = vendor_stores.payment_pay_link(venmo, 12.5, "ABC123")
        self.assertIn("venmo.com", link or "")
        self.assertIn("12.50", link or "")
        self.assertIn("ABC123", link or "")
        pp_user = payment_templates.render_paypal("unicornshop")
        self.assertIn(
            "paypal.me/",
            vendor_stores.payment_pay_link(pp_user, 10, "X") or "",
        )
        pp_email = payment_templates.render_paypal("unicornfartzz@proton.me")
        self.assertIsNone(vendor_stores.payment_pay_link(pp_email, 10, "X"))
        cash = payment_templates.render_cashapp("$tag")
        self.assertIn("cash.app", vendor_stores.payment_pay_link(cash, 3, "Z") or "")


if __name__ == "__main__":
    unittest.main()
