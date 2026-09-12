"""Unicorn payment-method seed + empty-rails guards (scratch DB only)."""

from __future__ import annotations

import json
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
        self.assertEqual(body["payments"]["usable"], 2)
        self.assertTrue(body["payments"]["checkout_ready"])
        self.assertEqual(
            body.get("store_url_cache_bust"), vendor_stores.STORE_URL_CACHE_BUST
        )
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
        self.assertTrue(state["shop"]["checkout_ready"])
        hints = [m.get("buyer_hint") or "" for m in state.get("payments") or []]
        self.assertTrue(any("Venmo" in h or "PayPal" in h or "Copy" in h for h in hints))
        self.assertFalse(any("wineboos" in h or "proton" in h for h in hints))
        other_code, other_state = webpanel.api_state(other_tok)
        self.assertEqual(other_code, 200)
        self.assertFalse(other_state["shop"]["is_unicorn"])
        self.assertFalse(other_state["shop"]["checkout_ready"])

    def test_storefront_payment_methods_name_and_type_only(self) -> None:
        webpanel.ensure_unicorn_shop_payments(UNICORN)
        webpanel.ensure_storefront_key_plain(UNICORN, PAGES_KEY)
        code, body = webpanel.api_storefront(PAGES_KEY)
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("checkout_ready"))
        self.assertIn("Pay after checkout", body.get("message") or "")
        self.assertFalse(body.get("invoices_enabled"))
        self.assertIn("Venmo", body["payments"])
        self.assertIn("PayPal", body["payments"])
        kinds = {m["method_type"] for m in body["payment_methods"]}
        self.assertEqual(kinds, {"venmo", "paypal"})
        blob = json.dumps(body)
        self.assertNotIn("wineboos", blob)
        self.assertNotIn("proton", blob)
        self.assertNotIn("pay_url", blob)
        self.assertNotIn("pay_hint", blob)
        for m in body["payment_methods"]:
            self.assertNotIn("handle", m)
            self.assertNotIn("instructions", m)
            self.assertNotIn("target", m)
            self.assertNotIn("pay_url", m)
            self.assertNotIn("pay_hint", m)

    def test_storefront_checkout_ready_false_when_paused(self) -> None:
        webpanel.ensure_unicorn_shop_payments(UNICORN)
        webpanel.ensure_storefront_key_plain(UNICORN, PAGES_KEY)
        for m in db.list_payment_methods(UNICORN, active_only=False):
            db.update_payment_method(m["id"], active=0)
        code, body = webpanel.api_storefront(PAGES_KEY)
        self.assertEqual(code, 200, body)
        self.assertFalse(body.get("checkout_ready"))
        self.assertIn("Message the seller", body.get("message") or "")
        self.assertNotIn("DM'd", body.get("message") or "")
        self.assertFalse(body.get("invoices_enabled"))
        self.assertEqual(body.get("payments"), [])
        self.assertEqual(body.get("payment_methods"), [])

    def test_empty_handle_typed_method_is_not_checkout_ready(self) -> None:
        db.add_payment_method(
            UNICORN, "Venmo", "", method_type="venmo", handle=""
        )
        webpanel.ensure_storefront_key_plain(UNICORN, PAGES_KEY)
        rows = db.list_payment_methods(UNICORN, active_only=False)
        self.assertEqual(len(rows), 1)
        self.assertFalse(vendor_stores.payment_rail_usable(rows[0]))
        self.assertFalse(vendor_stores.shop_checkout_ready(UNICORN))
        code, body = webpanel.api_storefront(PAGES_KEY)
        self.assertEqual(code, 200, body)
        self.assertFalse(body.get("checkout_ready"))
        self.assertEqual(body.get("payments"), [])
        self.assertEqual(body.get("payment_methods"), [])
        self.assertIn("Message the seller", body.get("message") or "")
        st, state = webpanel.api_state({"chat_id": UNICORN, "user_id": 1})
        self.assertEqual(st, 200)
        self.assertFalse(state["shop"]["checkout_ready"])
        self.assertFalse(state["payments"][0].get("rail_ready"))
        with mock.patch.object(
            unicorn_shop, "find_catalog_shop", return_value={"chat_id": UNICORN}
        ):
            health = spbc_notify._status_body()
        self.assertEqual(health["payments"]["active"], 1)
        self.assertEqual(health["payments"]["usable"], 0)
        self.assertFalse(health["payments"]["checkout_ready"])
        blob = str(health["payments"])
        self.assertNotIn("wineboos", blob)

    def test_empty_venmo_does_not_hide_usable_paypal(self) -> None:
        db.add_payment_method(
            UNICORN, "Venmo", "", method_type="venmo", handle=""
        )
        db.add_payment_method(
            UNICORN,
            "PayPal",
            "send to unicornfartzz@proton.me",
            method_type="paypal",
            handle="unicornfartzz@proton.me",
        )
        webpanel.ensure_storefront_key_plain(UNICORN, PAGES_KEY)
        self.assertTrue(vendor_stores.shop_checkout_ready(UNICORN))
        code, body = webpanel.api_storefront(PAGES_KEY)
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("checkout_ready"))
        self.assertEqual(body.get("payments"), ["PayPal"])
        kinds = {m["method_type"] for m in body["payment_methods"]}
        self.assertEqual(kinds, {"paypal"})
        blob = json.dumps(body)
        self.assertNotIn("proton", blob)

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
        import re

        import bot as bot_mod
        import payment_templates as pt_mod

        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('callback_data="adm_seedpays"', src)
        self.assertIn('callback_data="paytpl:paypal"', src)
        self.assertIn('callback_data="paytpl:apple_cash"', src)
        self.assertIn("PAY_TPL_CALLBACK_RE", src)
        self.assertIn("cb_adm_seedpays", src)
        for mt in pt_mod.METHOD_TYPES:
            self.assertRegex(
                f"paytpl:{mt}",
                bot_mod.PAY_TPL_CALLBACK_RE,
                msg=f"{mt} quick-add must match ConversationHandler",
            )
        self.assertIsNone(re.match(bot_mod.PAY_TPL_CALLBACK_RE, "paytpl:nope"))
        self.assertIn("Need a real", src)
        self.assertIn("payment_target_kind_label", src)
        self.assertNotIn("Need a real handle / cashtag", src)
        self.assertIn("_All methods paused._", src)
        self.assertIn("payment_empty_rails_admin_line", src)
        self.assertIn("_Buyers can checkout._", src)
        self.assertIn("add a pay target", src)
        self.assertIn("cashtag", src)
        self.assertIn("Wallet address", src)
        vs = (ROOT / "vendor_stores.py").read_text(encoding="utf-8")
        self.assertIn("_Enabled methods have no pay target._", vs)
        self.assertIn("PAYMENT_TARGET_COPY", vs)
        panel = (ROOT / "webpanel.py").read_text(encoding="utf-8")
        self.assertIn("types.has('venmo')", panel)
        self.assertIn("checkout_ready", panel)
        self.assertIn("Buyers see:", panel)
        self.assertIn("buyer_hint", panel)
        self.assertIn("rail_ready", panel)
        self.assertIn("empty_rail_hint", panel)
        self.assertIn("no pay target", panel)
        self.assertIn("Add a wallet address so buyers can use this method", panel)
        self.assertIn("a Cash App cashtag", panel)
        self.assertIn("_reject_unusable_enabled_payment", panel)
        self.assertIn("PAY_TARGET_COPY", panel)
        self.assertIn("p-handle-lab", panel)
        self.assertIn("target_kind_label", panel)
        self.assertIn("Zelle email or phone", panel)
        self.assertNotIn("Handle / email / phone", panel)
        self.assertNotIn("Zelle contact", panel)
        self.assertIn("edit pay targets anytime", panel)
        for mt, copy in vendor_stores.PAYMENT_TARGET_COPY.items():
            self.assertIn(copy["kind"], panel)
            self.assertIn(copy["kind"], vs)

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
        venmo_hint = vendor_stores.payment_pay_hint(venmo, 12.5, "ABC123")
        self.assertIn("Venmo", venmo_hint)
        self.assertIn("prefilled", venmo_hint)
        self.assertNotIn("wineboos", venmo_hint)
        email_hint = vendor_stores.payment_pay_hint(pp_email, 10, "X")
        self.assertIn("Friends & Family", email_hint)
        self.assertIn("Copy", email_hint)
        self.assertNotIn("proton", email_hint)
        self.assertFalse(vendor_stores.public_invoices_enabled())

    def test_empty_rail_copy_is_type_specific(self) -> None:
        venmo = {
            "name": "Venmo",
            "method_type": "venmo",
            "handle": "",
            "instructions": "",
        }
        crypto = {
            "name": "USDT",
            "method_type": "crypto",
            "address": "",
            "instructions": "",
        }
        cash = {
            "name": "Cash App",
            "method_type": "cashapp",
            "cashtag": "",
            "instructions": "",
        }
        apple = {
            "name": "Apple Cash",
            "method_type": "apple_cash",
            "handle": "",
            "instructions": "",
        }
        zelle = {
            "name": "Zelle",
            "method_type": "zelle",
            "handle": "",
            "instructions": "",
        }
        self.assertIn("Venmo handle", vendor_stores.payment_target_kind_label(venmo))
        self.assertIn("wallet address", vendor_stores.payment_target_kind_label(crypto))
        self.assertIn("cashtag", vendor_stores.payment_target_kind_label(cash))
        self.assertIn("Apple Cash number", vendor_stores.payment_target_kind_label(apple))
        self.assertIn("email or phone", vendor_stores.payment_target_kind_label(zelle))
        self.assertNotIn("contact", vendor_stores.payment_target_kind_label(zelle).lower())
        self.assertEqual(
            vendor_stores.payment_target_placeholder(cash), "$cashtag"
        )
        self.assertIn("wallet address", vendor_stores.payment_empty_rail_hint(crypto))
        self.assertNotIn("handle", vendor_stores.payment_empty_rail_hint(crypto).lower())
        self.assertIn(
            "cashtag",
            vendor_stores.payment_empty_rail_save_error(cash).lower(),
        )
        self.assertNotIn(
            "handle",
            vendor_stores.payment_empty_rail_save_error(cash).lower(),
        )
        self.assertIn(
            "Venmo handle",
            vendor_stores.payment_empty_rail_save_error(venmo),
        )
        banner = vendor_stores.payment_empty_rails_admin_line()
        self.assertIn("pay target", banner)
        self.assertNotIn("no handle", banner.lower())
        zelle_hint = vendor_stores.payment_pay_hint(zelle)
        self.assertIn("email or phone", zelle_hint)
        self.assertNotIn("contact", zelle_hint.lower())

    def test_payment_rail_usable_typed_vs_custom(self) -> None:
        venmo = payment_templates.render_venmo("@wineboos")
        self.assertTrue(vendor_stores.payment_rail_usable(venmo))
        empty_venmo = {
            "name": "Venmo",
            "method_type": "venmo",
            "handle": "",
            "instructions": "",
        }
        self.assertFalse(vendor_stores.payment_rail_usable(empty_venmo))
        custom = {
            "name": "Cash pickup",
            "method_type": "custom",
            "instructions": "Pay in person at pickup.",
        }
        self.assertTrue(vendor_stores.payment_rail_usable(custom))
        thin = {
            "name": "Custom",
            "method_type": "custom",
            "instructions": "n/a",
        }
        self.assertFalse(vendor_stores.payment_rail_usable(thin))
        from_instr = {
            "name": "Venmo",
            "method_type": "venmo",
            "handle": "",
            "instructions": "Send to @shop-venmo",
        }
        self.assertTrue(vendor_stores.payment_rail_usable(from_instr))

    def test_crypto_missing_network_warns_but_stays_usable(self) -> None:
        row = payment_templates.render_crypto("USDT", "Txyz123", "")
        self.assertTrue(vendor_stores.payment_rail_usable(row))
        warn = vendor_stores.payment_rail_warning(row)
        self.assertIn("network", warn.lower())
        noted = payment_templates.render_crypto("USDT", "Txyz123", "USDT TRC20")
        self.assertTrue(vendor_stores.payment_rail_usable(noted))
        self.assertEqual(vendor_stores.payment_rail_warning(noted), "")
        empty = {
            "name": "USDT",
            "method_type": "crypto",
            "address": "",
            "instructions": "",
        }
        self.assertFalse(vendor_stores.payment_rail_usable(empty))
        self.assertEqual(vendor_stores.payment_rail_warning(empty), "")

    def test_panel_400_empty_typed_save_is_type_specific(self) -> None:
        tok = {"chat_id": UNICORN, "user_id": 1}
        code, data = webpanel.api_payment(
            tok, {"method_type": "cashapp", "cashtag": "", "active": True}
        )
        self.assertEqual(code, 400, data)
        err = (data.get("error") or "").lower()
        self.assertIn("cashtag", err)
        self.assertNotIn("handle", err)
        code, data = webpanel.api_payment(
            tok, {"method_type": "crypto", "address": "", "active": True}
        )
        self.assertEqual(code, 400, data)
        self.assertIn("wallet", (data.get("error") or "").lower())
        code, data = webpanel.api_payment(
            tok, {"method_type": "zelle", "handle": "", "active": True}
        )
        self.assertEqual(code, 400, data)
        zerr = (data.get("error") or "").lower()
        self.assertIn("email or phone", zerr)
        self.assertNotIn("contact", zerr)
        code, state = webpanel.api_state(tok)
        self.assertEqual(code, 200, state)
        mid = db.add_payment_from_template(
            UNICORN, payment_templates.render_crypto("USDT", "Txyz", "")
        )
        code, state = webpanel.api_state(tok)
        self.assertEqual(code, 200)
        row = next(p for p in state["payments"] if int(p["id"]) == mid)
        self.assertTrue(row.get("rail_ready"))
        self.assertIn("network", (row.get("rail_warning") or "").lower())
        self.assertIn("wallet address", row.get("target_kind_label") or "")
        self.assertIn("0x", row.get("target_placeholder") or "")


if __name__ == "__main__":
    unittest.main()
