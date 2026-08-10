"""Per-order /track capability token + standalone form (scratch DB only)."""

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

SHOP = 95001
OTHER_SHOP = 95002
USER = 42
CUSTOMER = 88001


class OrderTrackingLinkBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "track_link.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Track Shop")
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


class MintResolveTests(OrderTrackingLinkBase):
    def test_mint_is_idempotent_per_order(self):
        o = self._order()
        a = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        b = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 24)  # token_hex(12)
        self.assertTrue(a.isalnum())

    def test_mint_different_orders_get_different_tokens(self):
        a = webpanel.mint_order_tracking_token(int(self._order()["id"]), SHOP)
        b = webpanel.mint_order_tracking_token(int(self._order()["id"]), SHOP)
        self.assertNotEqual(a, b)

    def test_resolve_rejects_unknown(self):
        self.assertIsNone(webpanel.resolve_order_tracking_token(""))
        self.assertIsNone(webpanel.resolve_order_tracking_token("deadbeef" * 3))

    def test_resolve_rejects_expired(self):
        o = self._order()
        raw = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        past = webpanel._ts(webpanel._utc_now() - timedelta(days=1))
        with db.get_db() as conn:
            conn.execute(
                "UPDATE order_action_tokens SET expires_at = ? WHERE token_plain = ?",
                (past, raw),
            )
        self.assertIsNone(webpanel.resolve_order_tracking_token(raw))

    def test_resolve_returns_order_and_shop(self):
        o = self._order()
        raw = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        got = webpanel.resolve_order_tracking_token(raw)
        self.assertEqual(
            got, {"order_id": int(o["id"]), "shop_chat_id": SHOP}
        )

    def test_separate_namespace_from_web_tokens(self):
        o = self._order()
        ot = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        # Order-action token is not a panel magic-link
        self.assertIsNone(webpanel.resolve_token(ot))
        panel = webpanel.issue_token(SHOP, USER)
        self.assertIsNone(webpanel.resolve_order_tracking_token(panel))


class TrackGetPostTests(OrderTrackingLinkBase):
    def test_get_renders_order_and_form(self):
        o = self._order()
        raw = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        code, ctype, body = webpanel.handle_track_get({"ot": [raw]})
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        text = body.decode("utf-8")
        self.assertIn(o["payment_code"], text)
        self.assertIn("BPC-157", text)
        self.assertIn("Buyer One", text)
        self.assertIn("1 Test St", text)
        self.assertIn("name=tracking_number", text)
        self.assertIn("name=carrier", text)
        self.assertIn("UPS", text)
        self.assertIn("USPS", text)
        self.assertIn(f'value="{raw}"', text)
        self.assertNotIn("expired", text.lower())

    def test_get_expired_for_bad_token(self):
        code, ctype, body = webpanel.handle_track_get({"ot": ["notarealtoken"]})
        self.assertEqual(code, 403)
        self.assertIn("text/html", ctype)
        self.assertIn("expired", body.decode("utf-8").lower())
        self.assertNotIn("tracking_number", body.decode("utf-8"))

    def test_post_sets_tracking_marks_shipped_notifies_customer(self):
        o = self._order()
        ok, msg, _ = db.confirm_order_payment(int(o["id"]), USER)
        self.assertTrue(ok, msg)
        raw = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        with mock.patch.object(
            webpanel, "notify_order_customer", return_value=True
        ) as dm:
            code, ctype, body = webpanel.handle_track_post(
                {
                    "ot": raw,
                    "carrier": "USPS",
                    "tracking_number": "9400111899223344556677",
                },
                wants_json=True,
            )
        self.assertEqual(code, 200, body)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "shipped")
        self.assertTrue(data["customer_notified"])
        got = db.get_order(int(o["id"]))
        self.assertEqual(got["status"], "shipped")
        self.assertEqual(got["tracking_number"], "9400111899223344556677")
        self.assertEqual(got["tracking_carrier"], "USPS")
        dm.assert_called_once()
        args = dm.call_args[0]
        self.assertEqual(args[0], SHOP)
        self.assertEqual(args[1], CUSTOMER)
        self.assertIn(o["payment_code"], args[2])
        self.assertIn("shipped", args[2].lower())
        self.assertIn("9400111899223344556677", args[2])

    def test_post_html_success_page(self):
        o = self._order()
        db.confirm_order_payment(int(o["id"]), USER)
        raw = webpanel.mint_order_tracking_token(int(o["id"]), SHOP)
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True):
            code, ctype, body = webpanel.handle_track_post(
                {
                    "ot": raw,
                    "carrier": "UPS",
                    "tracking_number": "1Z999AA10123456784",
                },
                wants_json=False,
            )
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("Tracking saved", body.decode("utf-8"))
        self.assertIn("1Z999AA10123456784", body.decode("utf-8"))

    def test_token_for_order_a_cannot_affect_order_b(self):
        a = self._order()
        b = self._order()
        db.confirm_order_payment(int(a["id"]), USER)
        db.confirm_order_payment(int(b["id"]), USER)
        raw_a = webpanel.mint_order_tracking_token(int(a["id"]), SHOP)
        with mock.patch.object(webpanel, "notify_order_customer", return_value=True):
            code, _, body = webpanel.handle_track_post(
                {
                    "ot": raw_a,
                    "carrier": "UPS",
                    "tracking_number": "TRACK-ONLY-A",
                    # attacker-supplied order_id must be ignored
                    "order_id": b["id"],
                },
                wants_json=True,
            )
        self.assertEqual(code, 200, body)
        got_a = db.get_order(int(a["id"]))
        got_b = db.get_order(int(b["id"]))
        self.assertEqual(got_a["tracking_number"], "TRACK-ONLY-A")
        self.assertEqual(got_a["status"], "shipped")
        self.assertFalse(got_b.get("tracking_number"))
        self.assertEqual(got_b["status"], "paid")

    def test_post_expired_token_rejected(self):
        o = self._order()
        db.confirm_order_payment(int(o["id"]), USER)
        with mock.patch.object(webpanel, "notify_order_customer") as dm:
            code, _, body = webpanel.handle_track_post(
                {
                    "ot": "bogus",
                    "carrier": "UPS",
                    "tracking_number": "1Z999",
                },
                wants_json=True,
            )
        self.assertEqual(code, 403)
        dm.assert_not_called()
        self.assertEqual(db.get_order(int(o["id"]))["status"], "paid")


class NewOrderDmLinkTests(OrderTrackingLinkBase):
    def test_dm_line_contains_track_ot_link(self):
        o = self._order()
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            line = webpanel.format_add_tracking_dm_line(int(o["id"]), SHOP)
        self.assertTrue(line.startswith("➕ Add tracking: "))
        self.assertIn("https://bot.example.com/track?ot=", line)
        raw = line.split("ot=", 1)[1]
        got = webpanel.resolve_order_tracking_token(raw)
        self.assertEqual(got["order_id"], int(o["id"]))
        self.assertEqual(got["shop_chat_id"], SHOP)

    def test_dm_line_skipped_when_no_panel_base_url(self):
        o = self._order()
        with mock.patch.object(webpanel, "PANEL_BASE_URL", ""):
            line = webpanel.format_add_tracking_dm_line(int(o["id"]), SHOP)
        self.assertEqual(line, "")


if __name__ == "__main__":
    unittest.main()
