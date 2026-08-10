"""POST /order — mini-app checkout via Telegram initData (scratch DB only)."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import spbc_notify  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP = 88001
OWNER = 77001
ADMIN = 77002
BUYER = 66001
VENDOR_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
OTHER_TOKEN = "999999999:BBBother-token-for-tamper-tests"


def build_valid_init_data(
    bot_token: str,
    *,
    user_id: int = BUYER,
    username: str = "buyer_user",
    first_name: str = "Buyer",
    last_name: str = "Bee",
    auth_date: int | None = None,
    extra: dict | None = None,
) -> str:
    """Build a Telegram WebApp initData string signed with the real algorithm."""
    auth = int(auth_date if auth_date is not None else time.time())
    user = {
        "id": int(user_id),
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "language_code": "en",
    }
    pairs: dict[str, str] = {
        "auth_date": str(auth),
        "query_id": "AAEAAAE_test_query",
        "user": json.dumps(user, separators=(",", ":")),
    }
    if extra:
        for k, v in extra.items():
            pairs[str(k)] = str(v)
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    pairs["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urllib.parse.urlencode(pairs)


class OrderHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "order_http.db")
        db.init_db()
        db.ensure_shop(SHOP, title="HTTP Order Shop")
        webpanel.ensure_webpanel_tables()
        self.sf_key = webpanel._ensure_storefront_key(SHOP)
        self.pid = db.add_product(SHOP, "BPC-157 5MG", 40.0, stock=20)
        db.add_payment_method(SHOP, "Venmo", "@shop-venmo memo CODE")
        db.add_admin(SHOP, ADMIN, "admin", OWNER)
        self.sent: list[tuple] = []

        def fake_vendor_send(token, chat_id, text, **kwargs):
            self.sent.append(("vendor", token, int(chat_id), text, kwargs))
            return True

        def fake_main_send(chat_id, text):
            self.sent.append(("main", int(chat_id), text))
            return {}

        self._patches = [
            mock.patch.object(
                vendor_stores,
                "get_bot_token_for_shop",
                return_value=VENDOR_TOKEN,
            ),
            mock.patch.object(
                vendor_stores,
                "base_notify_ids_for_shop",
                return_value=[OWNER],
            ),
            mock.patch.object(
                vendor_stores,
                "vendor_meta_for_shop",
                return_value={
                    "name": "HTTP Order Shop",
                    "emoji": "🧪",
                    "notify_ids": [OWNER],
                },
            ),
            mock.patch.object(
                webpanel, "telegram_send_with_token", side_effect=fake_vendor_send
            ),
            mock.patch.object(
                spbc_notify, "send_telegram", side_effect=fake_main_send
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _payload(self, **overrides) -> dict:
        body = {
            "invite": self.sf_key,
            "initData": build_valid_init_data(VENDOR_TOKEN),
            "items": [{"id": self.pid, "vials": 2, "kits": 0}],
            "ship": {
                "name": "Buyer Bee",
                "line1": "1 Test St",
                "line2": "",
                "city": "Austin",
                "state": "TX",
                "zip": "78701",
                "phone": "512-555-0100",
            },
        }
        body.update(overrides)
        return body

    def test_build_valid_init_data_roundtrip(self) -> None:
        raw = build_valid_init_data(VENDOR_TOKEN, user_id=42, username="x")
        buyer = vendor_stores.validate_webapp_init_data(raw, VENDOR_TOKEN)
        self.assertEqual(buyer["user_id"], 42)
        self.assertEqual(buyer["username"], "x")
        self.assertEqual(buyer["full_name"], "Buyer Bee")

    def test_valid_order_200_creates_order_notifies_and_confirms(self) -> None:
        code, body = spbc_notify.handle_http_order(self._payload())
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))
        self.assertIn("code", body)
        self.assertIsInstance(body["code"], str)
        self.assertGreater(len(body["code"]), 0)
        self.assertIsInstance(body["total"], float)
        self.assertGreater(body["total"], 0)
        self.assertIsInstance(body["payments"], list)
        self.assertTrue(any("Venmo" in p for p in body["payments"]))
        self.assertIn("Order received", body["message"])
        self.assertIn(body["code"], body["message"])
        self.assertIn("1 Test St", body["message"])

        # Exactly one order
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE chat_id = ?", (SHOP,)
            ).fetchall()
        self.assertEqual(len(rows), 1)
        order = dict(rows[0])
        self.assertEqual(int(order["user_id"]), BUYER)
        self.assertEqual(order["payment_code"], body["code"])
        self.assertEqual(float(order["total"]), body["total"])
        self.assertEqual(order["ship_name"], "Buyer Bee")
        self.assertIn("1 Test St", order["ship_address"] or "")

        # Buyer confirm + at least one owner/admin notify via vendor bot
        vendor_sends = [s for s in self.sent if s[0] == "vendor"]
        chat_ids = {s[2] for s in vendor_sends}
        self.assertIn(BUYER, chat_ids)
        # OWNER and/or ADMIN (admins folded into notify set)
        self.assertTrue(chat_ids & {OWNER, ADMIN})
        buyer_msg = next(s[3] for s in vendor_sends if s[2] == BUYER)
        self.assertEqual(buyer_msg, body["message"])
        # Vendor bot token used for buyer
        buyer_tok = next(s[1] for s in vendor_sends if s[2] == BUYER)
        self.assertEqual(buyer_tok, VENDOR_TOKEN)

    def test_tampered_init_data_401_no_order(self) -> None:
        good = build_valid_init_data(VENDOR_TOKEN)
        # Flip last hex char of hash
        if good.endswith("a"):
            bad = good[:-1] + "b"
        else:
            bad = good[:-1] + "a"
        code, body = spbc_notify.handle_http_order(
            self._payload(initData=bad)
        )
        self.assertEqual(code, 401)
        self.assertFalse(body.get("ok"))
        self.assertIn("initdata", body.get("error", "").lower())
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual(n, 0)
        self.assertEqual(self.sent, [])

    def test_wrong_token_signature_401(self) -> None:
        # Signed with a different bot token → 401
        forged = build_valid_init_data(OTHER_TOKEN)
        code, body = spbc_notify.handle_http_order(
            self._payload(initData=forged)
        )
        self.assertEqual(code, 401)
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_expired_auth_date_401(self) -> None:
        old = int(time.time()) - (25 * 60 * 60)
        expired = build_valid_init_data(VENDOR_TOKEN, auth_date=old)
        code, body = spbc_notify.handle_http_order(
            self._payload(initData=expired)
        )
        self.assertEqual(code, 401)
        self.assertFalse(body.get("ok"))
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_unknown_storefront_404(self) -> None:
        code, body = spbc_notify.handle_http_order(
            self._payload(invite="deadbeefdeadbeefdeadbeef")
        )
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        self.assertIn("storefront", body.get("error", "").lower())
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_empty_cart_400(self) -> None:
        code, body = spbc_notify.handle_http_order(
            self._payload(items=[])
        )
        self.assertEqual(code, 400)
        self.assertFalse(body.get("ok"))
        self.assertIn("empty", body.get("error", "").lower())
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_stock_insufficient_409(self) -> None:
        code, body = spbc_notify.handle_http_order(
            self._payload(items=[{"id": self.pid, "vials": 999, "kits": 0}])
        )
        self.assertEqual(code, 409)
        self.assertFalse(body.get("ok"))
        self.assertIn("sold", body.get("error", "").lower())
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_bad_json_shape_400(self) -> None:
        code, body = spbc_notify.handle_http_order(
            self._payload(items="not-a-list")
        )
        self.assertEqual(code, 400)
        self.assertFalse(body.get("ok"))

    def test_claim_token_not_accepted_as_invite(self) -> None:
        """storefront_keys separation: claim invite must not resolve."""
        claim = webpanel.create_vendor_invite(OWNER, "should-not-work")
        # Normalize may strip vendor prefix; raw claim is not a storefront key
        code, body = spbc_notify.handle_http_order(self._payload(invite=claim))
        self.assertEqual(code, 404)
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_options_order_cors_headers(self) -> None:
        handler = object.__new__(spbc_notify.NotifyHTTPHandler)
        handler.path = "/order"
        captured: dict = {"headers": [], "code": None}

        def send_response(code, *a, **k):
            captured["code"] = code

        def send_header(name, value):
            captured["headers"].append((name, value))

        def end_headers():
            captured["ended"] = True

        handler.send_response = send_response  # type: ignore[method-assign]
        handler.send_header = send_header  # type: ignore[method-assign]
        handler.end_headers = end_headers  # type: ignore[method-assign]
        handler.do_OPTIONS()
        self.assertEqual(captured["code"], 204)
        hdrs = {k.lower(): v for k, v in captured["headers"]}
        self.assertEqual(hdrs.get("access-control-allow-origin"), "*")
        self.assertIn("POST", hdrs.get("access-control-allow-methods", ""))
        self.assertIn("OPTIONS", hdrs.get("access-control-allow-methods", ""))
        self.assertIn(
            "Content-Type", hdrs.get("access-control-allow-headers", "")
        )

    def test_post_order_response_includes_cors(self) -> None:
        """Handler do_POST /order writes CORS on success JSON."""
        handler = object.__new__(spbc_notify.NotifyHTTPHandler)
        handler.path = "/order"
        body_bytes = json.dumps(self._payload()).encode("utf-8")
        handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
        }
        handler.rfile = io.BytesIO(body_bytes)
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
        handler.do_POST()
        self.assertEqual(responses, [200])
        hdrs = {k.lower(): v for k, v in headers}
        self.assertEqual(hdrs.get("access-control-allow-origin"), "*")
        written = out.getvalue().decode("utf-8")
        data = json.loads(written)
        self.assertTrue(data.get("ok"))
        self.assertIn("code", data)
        self.assertIn("total", data)
        self.assertIn("payments", data)
        self.assertIn("message", data)


if __name__ == "__main__":
    unittest.main()
