"""Per-order /confirm capability token + standalone form (scratch DB only)."""

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
import webpanel  # noqa: E402

SHOP = 96001
OTHER_SHOP = 96002
USER = 42
CUSTOMER = 88011


class OrderConfirmLinkBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "confirm_link.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Confirm Shop")
        db.ensure_shop(OTHER_SHOP, title="Other Shop")
        webpanel.ensure_webpanel_tables()
        self.pid = db.add_product(SHOP, "BPC-157 10MG", 41.0, stock=10)
        self.other_pid = db.add_product(OTHER_SHOP, "Foreign", 9.0, stock=5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _order(
        self,
        shop: int = SHOP,
        pid: int | None = None,
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
                    "quantity": 1,
                }
            ],
            {"id": None, "name": "Cash App"},
            "Buyer One",
            "1 Test St\nSpringfield, IL 62701",
            "",
        )
        self.assertIsNotNone(o)
        return o


class MintResolveTests(OrderConfirmLinkBase):
    def test_mint_is_idempotent_per_order(self):
        o = self._order()
        a = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        b = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 24)  # token_hex(12)
        self.assertTrue(a.isalnum())

    def test_confirm_token_distinct_from_track_token(self):
        o = self._order()
        ct = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        ot = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        self.assertNotEqual(ct, ot)
        self.assertEqual(
            webpanel.resolve_order_confirm_token(ct),
            {"order_id": int(o["id"]), "shop_chat_id": SHOP},
        )
        self.assertIsNone(webpanel.resolve_order_confirm_token(ot))
        self.assertIsNone(webpanel.resolve_order_tracking_token(ct))
        self.assertEqual(
            webpanel.resolve_order_tracking_token(ot),
            {"order_id": int(o["id"]), "shop_chat_id": SHOP},
        )

    def test_mint_different_orders_get_different_tokens(self):
        a = webpanel.mint_order_confirm_token(int(self._order()["id"]), SHOP)
        b = webpanel.mint_order_confirm_token(int(self._order()["id"]), SHOP)
        self.assertNotEqual(a, b)

    def test_resolve_rejects_unknown(self):
        self.assertIsNone(webpanel.resolve_order_confirm_token(""))
        self.assertIsNone(webpanel.resolve_order_confirm_token("deadbeef" * 3))

    def test_resolve_rejects_expired(self):
        o = self._order()
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        past = webpanel._ts(webpanel._utc_now() - timedelta(days=1))
        with db.get_db() as conn:
            conn.execute(
                "UPDATE order_action_tokens SET expires_at = ? WHERE token_plain = ?",
                (past, raw),
            )
        self.assertIsNone(webpanel.resolve_order_confirm_token(raw))

    def test_resolve_returns_order_and_shop(self):
        o = self._order()
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        got = webpanel.resolve_order_confirm_token(raw)
        self.assertEqual(
            got, {"order_id": int(o["id"]), "shop_chat_id": SHOP}
        )

    def test_separate_namespace_from_web_tokens(self):
        o = self._order()
        ct = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        self.assertIsNone(webpanel.resolve_token(ct))
        panel = webpanel.issue_token(SHOP, USER)
        self.assertIsNone(webpanel.resolve_order_confirm_token(panel))


class ConfirmGetPostTests(OrderConfirmLinkBase):
    def test_get_renders_order_and_button_for_pending(self):
        o = self._order()
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        code, ctype, body = webpanel.handle_confirm_get({"ct": [raw]})
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        text = body.decode("utf-8")
        self.assertIn(o["payment_code"], text)
        self.assertIn("BPC-157", text)
        self.assertIn("Buyer One", text)
        self.assertIn("1 Test St", text)
        self.assertIn("Confirm payment received", text)
        self.assertIn(f'value="{raw}"', text)
        self.assertIn("name=ct", text)
        self.assertNotIn("Already confirmed", text)
        self.assertNotIn("expired", text.lower())

    def test_get_already_confirmed_for_paid(self):
        o = self._order()
        ok, msg, _ = db.confirm_order_payment(int(o["id"]), USER)
        self.assertTrue(ok, msg)
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        code, ctype, body = webpanel.handle_confirm_get({"ct": [raw]})
        self.assertEqual(code, 200)
        text = body.decode("utf-8")
        self.assertIn("Already confirmed", text)
        self.assertNotIn("Confirm payment received", text)
        self.assertNotIn("method=POST", text.lower())

    def test_get_expired_for_bad_token(self):
        code, ctype, body = webpanel.handle_confirm_get({"ct": ["notarealtoken"]})
        self.assertEqual(code, 403)
        self.assertIn("text/html", ctype)
        self.assertIn("expired", body.decode("utf-8").lower())
        self.assertNotIn("Confirm payment received", body.decode("utf-8"))

    def test_post_confirms_once_decrements_stock_notifies_customer(self):
        o = self._order()
        stock_before = db.get_product(self.pid)["stock"]
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        with mock.patch.object(
            webpanel, "notify_order_customer", return_value=True
        ) as dm:
            code, ctype, body = webpanel.handle_confirm_post(
                {"ct": raw},
                wants_json=True,
            )
        self.assertEqual(code, 200, body)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertFalse(data.get("already_confirmed"))
        self.assertEqual(data["status"], "paid")
        self.assertTrue(data["customer_notified"])
        got = db.get_order(int(o["id"]))
        self.assertEqual(got["status"], "paid")
        stock_after = db.get_product(self.pid)["stock"]
        self.assertEqual(stock_after, stock_before - 1)
        dm.assert_called_once()
        args = dm.call_args[0]
        self.assertEqual(args[0], SHOP)
        self.assertEqual(args[1], CUSTOMER)
        self.assertIn(o["payment_code"], args[2])
        self.assertIn("Payment received", args[2])

    def test_post_html_success_page(self):
        o = self._order()
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True):
            code, ctype, body = webpanel.handle_confirm_post(
                {"ct": raw},
                wants_json=False,
            )
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        text = body.decode("utf-8")
        self.assertIn("Payment confirmed", text)
        self.assertIn(o["payment_code"], text)

    def test_second_post_does_not_double_decrement(self):
        o = self._order()
        stock_before = db.get_product(self.pid)["stock"]
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True) as dm:
            code1, _, body1 = webpanel.handle_confirm_post(
                {"ct": raw}, wants_json=True
            )
            code2, _, body2 = webpanel.handle_confirm_post(
                {"ct": raw}, wants_json=True
            )
        self.assertEqual(code1, 200, body1)
        self.assertEqual(code2, 200, body2)
        data2 = json.loads(body2.decode("utf-8"))
        self.assertTrue(data2["ok"])
        self.assertTrue(data2.get("already_confirmed"))
        stock_after = db.get_product(self.pid)["stock"]
        self.assertEqual(stock_after, stock_before - 1)
        # customer notified only on first confirm
        self.assertEqual(dm.call_count, 1)

    def test_token_for_order_a_cannot_confirm_order_b(self):
        a = self._order()
        b = self._order()
        stock_before = db.get_product(self.pid)["stock"]
        raw_a = webpanel.mint_order_confirm_token(int(a["id"]), SHOP)
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True):
            code, _, body = webpanel.handle_confirm_post(
                {
                    "ct": raw_a,
                    # attacker-supplied order_id must be ignored
                    "order_id": b["id"],
                },
                wants_json=True,
            )
        self.assertEqual(code, 200, body)
        got_a = db.get_order(int(a["id"]))
        got_b = db.get_order(int(b["id"]))
        self.assertEqual(got_a["status"], "paid")
        self.assertIn(got_b["status"], ("pending_payment", "awaiting_confirmation"))
        # only one unit decremented (order A only)
        self.assertEqual(db.get_product(self.pid)["stock"], stock_before - 1)

    def test_post_expired_token_rejected(self):
        o = self._order()
        stock_before = db.get_product(self.pid)["stock"]
        with mock.patch.object(webpanel, "notify_order_customer") as dm:
            code, _, body = webpanel.handle_confirm_post(
                {"ct": "bogus"},
                wants_json=True,
            )
        self.assertEqual(code, 403)
        dm.assert_not_called()
        self.assertEqual(db.get_product(self.pid)["stock"], stock_before)
        self.assertIn(
            db.get_order(int(o["id"]))["status"],
            ("pending_payment", "awaiting_confirmation"),
        )

    def test_post_insufficient_stock_surfaces_message(self):
        o = self._order()
        # Drain stock so confirm fails
        with db.get_db() as conn:
            conn.execute(
                "UPDATE products SET stock = 0 WHERE id = ?", (self.pid,)
            )
        raw = webpanel.mint_order_confirm_token(int(o["id"]), SHOP)
        with mock.patch.object(webpanel, "notify_order_customer") as dm:
            code, ctype, body = webpanel.handle_confirm_post(
                {"ct": raw}, wants_json=False
            )
        self.assertEqual(code, 400)
        text = body.decode("utf-8")
        self.assertIn("Insufficient stock", text)
        dm.assert_not_called()
        self.assertIn(
            db.get_order(int(o["id"]))["status"],
            ("pending_payment", "awaiting_confirmation"),
        )


class NewOrderDmLinkTests(OrderConfirmLinkBase):
    def test_dm_line_contains_confirm_ct_link(self):
        o = self._order()
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            line = webpanel.format_confirm_payment_dm_line(int(o["id"]), SHOP)
        self.assertTrue(line.startswith("✅ Confirm payment: "))
        self.assertIn("https://bot.example.com/confirm?ct=", line)
        raw = line.split("ct=", 1)[1]
        got = webpanel.resolve_order_confirm_token(raw)
        self.assertEqual(got["order_id"], int(o["id"]))
        self.assertEqual(got["shop_chat_id"], SHOP)

    def test_dm_line_skipped_when_no_panel_base_url(self):
        o = self._order()
        with mock.patch.object(webpanel, "PANEL_BASE_URL", ""):
            line = webpanel.format_confirm_payment_dm_line(int(o["id"]), SHOP)
        self.assertEqual(line, "")

    def test_new_order_text_has_both_confirm_and_track_links(self):
        o = self._order()
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            confirm = webpanel.format_confirm_payment_dm_line(int(o["id"]), SHOP)
            track = webpanel.format_add_tracking_dm_line(int(o["id"]), SHOP)
        note = (
            f"NEW ORDER {o['payment_code']}\n"
            + (confirm + "\n" if confirm else "")
            + (track if track else "")
        )
        self.assertIn("/confirm?ct=", note)
        self.assertIn("/track?ot=", note)
        self.assertIn("✅ Confirm payment:", note)
        self.assertIn("➕ Add tracking:", note)
        ct = confirm.split("ct=", 1)[1]
        ot = track.split("ot=", 1)[1]
        self.assertNotEqual(ct, ot)


if __name__ == "__main__":
    unittest.main()
