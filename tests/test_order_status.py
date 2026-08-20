"""GET /order-status — read-only, shop-scoped, storefront_keys only."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import spbc_notify  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP_A = 71001
SHOP_B = 71002
OWNER = 72001
BUYER = 73001


class OrderStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "ostatus.db")
        db.init_db()
        db.ensure_shop(SHOP_A, title="Shop A")
        db.ensure_shop(SHOP_B, title="Shop B")
        webpanel.ensure_webpanel_tables()
        self.sf_a = webpanel._ensure_storefront_key(SHOP_A)
        self.sf_b = webpanel._ensure_storefront_key(SHOP_B)
        self.pid = db.add_product(SHOP_A, "BPC-157", 40.0, stock=8)
        self.order = db.create_order(
            SHOP_A,
            BUYER,
            "buyer",
            "Buyer Bee",
            [
                {
                    "product_id": self.pid,
                    "product_name": "BPC-157",
                    "unit_price": 40.0,
                    "quantity": 1,
                }
            ],
            {"id": None, "name": "Venmo"},
            "Buyer Bee",
            "1 Test St, Austin TX",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_status_returns_items_no_internal_pii(self) -> None:
        code, body = webpanel.api_order_status(
            self.sf_a, self.order["payment_code"]
        )
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "pending_payment")
        self.assertEqual(body["code"], self.order["payment_code"])
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["name"], "BPC-157")
        self.assertEqual(body["ship_name"], "Buyer Bee")
        self.assertIn("1 Test St", body["ship_address"])
        self.assertNotIn("user_id", body)
        self.assertNotIn("username", body)
        self.assertNotIn("admin_note", body)
        self.assertNotIn("confirmed_by", body)
        self.assertNotIn("hidden_service_fee", body)

    def test_other_shop_key_cannot_see_order(self) -> None:
        code, body = webpanel.api_order_status(
            self.sf_b, self.order["payment_code"]
        )
        self.assertEqual(code, 404, body)
        self.assertFalse(body.get("ok"))

    def test_claim_token_rejected(self) -> None:
        claim = webpanel.create_vendor_invite(OWNER, "nope")
        code, body = webpanel.api_order_status(claim, self.order["payment_code"])
        self.assertEqual(code, 404, body)

    def test_unknown_code(self) -> None:
        code, body = webpanel.api_order_status(self.sf_a, "🎁999999")
        self.assertEqual(code, 404, body)

    def test_http_get_cors(self) -> None:
        handler = object.__new__(spbc_notify.NotifyHTTPHandler)
        q = f"invite={self.sf_a}&code={self.order['payment_code']}"
        handler.path = f"/order-status?{q}"
        out = io.BytesIO()
        handler.wfile = out  # type: ignore[assignment]
        responses: list[int] = []
        headers: list[tuple[str, str]] = []

        def send_response(code, *a, **k):
            responses.append(code)

        def send_header(name, value):
            headers.append((name, value))

        def end_headers():
            pass

        handler.send_response = send_response  # type: ignore[method-assign]
        handler.send_header = send_header  # type: ignore[method-assign]
        handler.end_headers = end_headers  # type: ignore[method-assign]
        handler.do_GET()
        self.assertEqual(responses, [200])
        hdrs = {k.lower(): v for k, v in headers}
        self.assertEqual(hdrs.get("access-control-allow-origin"), "*")
        data = json.loads(out.getvalue().decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["status"], "pending_payment")


class HttpOrderReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "http_res.db")
        db.init_db()
        db.ensure_shop(SHOP_A, title="HTTP Res Shop")
        webpanel.ensure_webpanel_tables()
        self.sf = webpanel._ensure_storefront_key(SHOP_A)
        self.pid = db.add_product(SHOP_A, "Item", 10.0, stock=2)
        self._patches = [
            mock.patch.object(
                vendor_stores, "get_bot_token_for_shop", return_value="tok"
            ),
            mock.patch.object(
                vendor_stores,
                "validate_webapp_init_data",
                return_value={
                    "user_id": BUYER,
                    "username": "b",
                    "full_name": "B",
                },
            ),
            mock.patch.object(
                vendor_stores, "base_notify_ids_for_shop", return_value=[]
            ),
            mock.patch.object(
                vendor_stores,
                "vendor_meta_for_shop",
                return_value={"name": "S", "emoji": "x", "notify_ids": []},
            ),
            mock.patch.object(webpanel, "telegram_send_with_token", return_value=True),
            mock.patch.object(spbc_notify, "send_telegram", return_value={}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_post_order_reserves_without_deduct_and_409s_oversell(self) -> None:
        payload = {
            "invite": self.sf,
            "initData": "ignored",
            "items": [{"id": self.pid, "vials": 2, "kits": 0}],
        }
        code, body = spbc_notify.handle_http_order(payload)
        self.assertEqual(code, 200, body)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 2)
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
            holds = conn.execute(
                "SELECT COUNT(*) c FROM stock_reservations WHERE released_at IS NULL"
            ).fetchone()["c"]
        self.assertEqual(n, 1)
        self.assertEqual(holds, 1)

        code2, body2 = spbc_notify.handle_http_order(
            {
                "invite": self.sf,
                "initData": "ignored",
                "items": [{"id": self.pid, "vials": 1, "kits": 0}],
            }
        )
        self.assertEqual(code2, 409, body2)
        with db.get_db() as conn:
            n2 = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        self.assertEqual(n2, 1)
        self.assertEqual(int(db.get_product(self.pid)["stock"]), 2)


if __name__ == "__main__":
    unittest.main()
