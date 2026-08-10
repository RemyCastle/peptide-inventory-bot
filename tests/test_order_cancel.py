"""Vendor cancel_order_any: pending (no stock) + paid (restock) + panel + DM link.

Scratch DB only — never touches inventory.db.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP = 97001
OTHER_SHOP = 97002
USER = 42
CUSTOMER = 88022


class CancelBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "cancel_order.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Cancel Shop")
        db.ensure_shop(OTHER_SHOP, title="Other Shop")
        webpanel.ensure_webpanel_tables()
        self.tok = {"chat_id": SHOP, "user_id": USER}
        self.pid = db.add_product(SHOP, "BPC-157 10MG", 41.0, stock=10)
        self.other_pid = db.add_product(OTHER_SHOP, "Foreign", 9.0, stock=5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _order(
        self,
        shop: int = SHOP,
        pid: int | None = None,
        qty: int = 2,
        user_id: int = CUSTOMER,
    ) -> dict:
        o = db.create_order(
            shop,
            user_id,
            "buyer1",
            "Buyer One",
            [
                {
                    "product_id": pid
                    if pid is not None
                    else (self.pid if shop == SHOP else self.other_pid),
                    "product_name": "BPC-157 10MG",
                    "unit_price": 41.0,
                    "quantity": qty,
                }
            ],
            {"id": None, "name": "Cash App"},
            "Buyer One",
            "1 Test St\nSpringfield, IL 62701",
            "",
        )
        self.assertIsNotNone(o)
        return o


class CancelOrderAnyDbTests(CancelBase):
    def test_cancel_pending_status_only_stock_unchanged(self):
        o = self._order(qty=2)
        stock_before = int(db.get_product(self.pid)["stock"])
        ok, msg = db.cancel_order_any(int(o["id"]), USER)
        self.assertTrue(ok, msg)
        self.assertIn("unchanged", msg.lower())
        got = db.get_order(int(o["id"]))
        self.assertEqual(got["status"], "cancelled")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock_before)
        self.assertEqual(db.list_stock_audit(product_id=self.pid), [])

    def test_cancel_awaiting_confirmation_no_stock_change(self):
        o = self._order(qty=1)
        db.mark_order_awaiting_confirmation(int(o["id"]))
        stock_before = int(db.get_product(self.pid)["stock"])
        ok, msg = db.cancel_order_any(int(o["id"]), USER)
        self.assertTrue(ok, msg)
        self.assertEqual(db.get_order(int(o["id"]))["status"], "cancelled")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock_before)

    def test_cancel_paid_restores_stock_and_audits(self):
        o = self._order(qty=3)
        stock0 = int(db.get_product(self.pid)["stock"])
        ok, msg, _ = db.confirm_order_payment(int(o["id"]), USER)
        self.assertTrue(ok, msg)
        stock_after_pay = int(db.get_product(self.pid)["stock"])
        self.assertEqual(stock_after_pay, stock0 - 3)

        ok2, msg2 = db.cancel_order_any(int(o["id"]), USER)
        self.assertTrue(ok2, msg2)
        self.assertIn("restored", msg2.lower())
        self.assertEqual(db.get_order(int(o["id"]))["status"], "cancelled")
        stock_final = int(db.get_product(self.pid)["stock"])
        self.assertEqual(stock_final, stock0)

        audits = db.list_stock_audit(product_id=self.pid)
        restock = [a for a in audits if a["reason"] == "cancel_restock"]
        self.assertEqual(len(restock), 1)
        row = restock[0]
        self.assertEqual(int(row["delta"]), 3)
        self.assertEqual(int(row["stock_before"]), stock_after_pay)
        self.assertEqual(int(row["stock_after"]), stock0)
        self.assertEqual(int(row["order_id"]), int(o["id"]))
        self.assertEqual(int(row["actor_id"]), USER)

    def test_cancel_shipped_restores_stock(self):
        o = self._order(qty=1)
        stock0 = int(db.get_product(self.pid)["stock"])
        db.confirm_order_payment(int(o["id"]), USER)
        db.set_order_tracking(int(o["id"]), "9400111", "USPS")
        db.mark_order_shipped(int(o["id"]))
        self.assertEqual(db.get_order(int(o["id"]))["status"], "shipped")
        ok, msg = db.cancel_order_any(int(o["id"]), USER)
        self.assertTrue(ok, msg)
        self.assertEqual(db.get_order(int(o["id"]))["status"], "cancelled")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock0)

    def test_already_cancelled_is_friendly_noop(self):
        o = self._order()
        ok1, _ = db.cancel_order_any(int(o["id"]), USER)
        self.assertTrue(ok1)
        stock = int(db.get_product(self.pid)["stock"])
        ok2, msg2 = db.cancel_order_any(int(o["id"]), USER)
        self.assertTrue(ok2, msg2)
        self.assertIn("already", msg2.lower())
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock)

    def test_legacy_cancel_order_still_refuses_paid(self):
        o = self._order()
        db.confirm_order_payment(int(o["id"]), USER)
        # customer-scoped cancel_order still refuses paid (use cancel_order_any)
        ok, msg = db.cancel_order(int(o["id"]), CUSTOMER)
        self.assertFalse(ok)
        self.assertIn("paid", msg.lower())
        self.assertEqual(db.get_order(int(o["id"]))["status"], "paid")


class ApiCancelOrderTests(CancelBase):
    def test_api_cancel_pending_and_dms_customer(self):
        o = self._order(qty=1)
        stock_before = int(db.get_product(self.pid)["stock"])
        with mock.patch.object(
            webpanel, "notify_order_customer", return_value=True
        ) as dm:
            code, data = webpanel.api_cancel_order(
                self.tok, {"order_id": o["id"]}
            )
        self.assertEqual(code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "cancelled")
        self.assertTrue(data["customer_notified"])
        self.assertEqual(db.get_order(int(o["id"]))["status"], "cancelled")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock_before)
        dm.assert_called_once()
        args = dm.call_args[0]
        self.assertEqual(args[0], SHOP)
        self.assertEqual(args[1], CUSTOMER)
        self.assertIn(o["payment_code"], args[2])
        self.assertIn("cancelled", args[2].lower())
        self.assertNotIn("refund", args[2].lower())

    def test_api_cancel_paid_restores_and_mentions_refund(self):
        o = self._order(qty=2)
        stock0 = int(db.get_product(self.pid)["stock"])
        db.confirm_order_payment(int(o["id"]), USER)
        with mock.patch.object(
            webpanel, "notify_order_customer", return_value=True
        ) as dm:
            code, data = webpanel.api_cancel_order(
                self.tok, {"order_id": o["id"]}
            )
        self.assertEqual(code, 200, data)
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock0)
        body = dm.call_args[0][2]
        self.assertIn("refund", body.lower())

    def test_api_cancel_rejects_cross_shop_404(self):
        foreign = self._order(shop=OTHER_SHOP, pid=self.other_pid)
        stock_f = int(db.get_product(self.other_pid)["stock"])
        code, data = webpanel.api_cancel_order(
            self.tok, {"order_id": foreign["id"]}
        )
        self.assertEqual(code, 404, data)
        self.assertFalse(data["ok"])
        self.assertEqual(
            db.get_order(int(foreign["id"]))["status"], "pending_payment"
        )
        self.assertEqual(int(db.get_product(self.other_pid)["stock"]), stock_f)

    def test_panel_html_has_cancel_button_and_confirm_prompt(self):
        html = webpanel.PANEL_HTML
        self.assertIn("b-cancel", html)
        self.assertIn("cancel_order", html)
        self.assertIn("Cancel this order? This cannot be undone.", html)
        self.assertIn("Cancel order", html)


class CancelTokenTests(CancelBase):
    def test_mint_idempotent_and_distinct_from_confirm_track(self):
        o = self._order()
        a = webpanel.mint_order_cancel_token(int(o["id"]), SHOP)
        b = webpanel.mint_order_cancel_token(int(o["id"]), SHOP)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 24)
        ct = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        ot = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        self.assertNotEqual(a, ct)
        self.assertNotEqual(a, ot)
        self.assertEqual(
            webpanel.resolve_order_cancel_token(a),
            {"order_id": int(o["id"]), "shop_chat_id": SHOP},
        )
        self.assertIsNone(webpanel.resolve_order_cancel_token(ct))
        self.assertIsNone(webpanel.resolve_order_confirm_token(a))

    def test_token_is_single_order_a_cannot_cancel_b(self):
        a = self._order()
        b = self._order()
        raw_a = webpanel.mint_order_cancel_token(int(a["id"]), SHOP)
        tok = webpanel.resolve_order_cancel_token(raw_a)
        self.assertEqual(tok["order_id"], int(a["id"]))
        self.assertNotEqual(tok["order_id"], int(b["id"]))
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True):
            code, _ctype, body = webpanel.handle_cancel_post(
                {"xt": raw_a}, wants_json=True
            )
        self.assertEqual(code, 200, body)
        self.assertEqual(db.get_order(int(a["id"]))["status"], "cancelled")
        self.assertEqual(db.get_order(int(b["id"]))["status"], "pending_payment")

    def test_get_is_two_step_form_not_one_tap(self):
        o = self._order()
        raw = webpanel.mint_order_cancel_token(int(o["id"]), SHOP)
        code, ctype, body = webpanel.handle_cancel_get({"xt": [raw]})
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        text = body.decode("utf-8")
        self.assertIn(o["payment_code"], text)
        self.assertIn("BPC-157", text)
        self.assertIn("Cancel this order", text)
        low = text.lower().replace('"', "")
        self.assertIn("method=post", low)
        self.assertIn("action=/cancel", low)
        self.assertIn(f'value="{raw}"', text)
        self.assertIn("name=xt", text)
        # GET alone must not cancel
        self.assertEqual(db.get_order(int(o["id"]))["status"], "pending_payment")

    def test_get_expired_for_bad_token(self):
        code, _ctype, body = webpanel.handle_cancel_get({"xt": ["notareal"]})
        self.assertEqual(code, 403)
        self.assertIn("expired", body.decode("utf-8").lower())

    def test_post_cancels_pending_and_notifies(self):
        o = self._order(qty=1)
        stock_before = int(db.get_product(self.pid)["stock"])
        raw = webpanel.mint_order_cancel_token(int(o["id"]), SHOP)
        with mock.patch.object(
            webpanel, "notify_order_customer", return_value=True
        ) as dm:
            code, _ctype, body = webpanel.handle_cancel_post(
                {"xt": raw}, wants_json=True
            )
        self.assertEqual(code, 200, body)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(db.get_order(int(o["id"]))["status"], "cancelled")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock_before)
        dm.assert_called_once()
        self.assertIn("cancelled", dm.call_args[0][2].lower())

    def test_post_cancels_paid_restores_stock(self):
        o = self._order(qty=2)
        stock0 = int(db.get_product(self.pid)["stock"])
        db.confirm_order_payment(int(o["id"]), USER)
        raw = webpanel.mint_order_cancel_token(int(o["id"]), SHOP)
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True):
            code, _ctype, body = webpanel.handle_cancel_post(
                {"xt": raw}, wants_json=True
            )
        self.assertEqual(code, 200, body)
        self.assertEqual(db.get_order(int(o["id"]))["status"], "cancelled")
        self.assertEqual(int(db.get_product(self.pid)["stock"]), stock0)
        restock = [
            a
            for a in db.list_stock_audit(product_id=self.pid)
            if a["reason"] == "cancel_restock"
        ]
        self.assertEqual(len(restock), 1)

    def test_resolve_rejects_expired(self):
        o = self._order()
        raw = webpanel.mint_order_cancel_token(int(o["id"]), SHOP)
        past = webpanel._ts(webpanel._utc_now() - timedelta(days=1))
        with db.get_db() as conn:
            conn.execute(
                "UPDATE order_action_tokens SET expires_at = ? WHERE token_plain = ?",
                (past, raw),
            )
        self.assertIsNone(webpanel.resolve_order_cancel_token(raw))


class NewOrderNotifyCancelLinkTests(CancelBase):
    def test_builder_includes_confirm_track_and_cancel(self):
        o = self._order()
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            note = vendor_stores.build_new_order_notify_text(
                o, shop_name="Cancel Shop"
            )
        self.assertIn("✅ Confirm payment:", note)
        self.assertIn("/confirm?ct=", note)
        self.assertIn("➕ Add tracking:", note)
        self.assertIn("/track?ot=", note)
        self.assertIn("❌ Cancel order:", note)
        self.assertIn("/cancel?xt=", note)


if __name__ == "__main__":
    unittest.main()
