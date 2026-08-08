"""Vendor web panel: tokens, invites, and the JSON API."""

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
import webpanel  # noqa: E402

SHOP = 900
USER = 42


class WebPanelBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "panel.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Vendor Shop")
        webpanel.ensure_webpanel_tables()
        self.tok = {"chat_id": SHOP, "user_id": USER}

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TokenTests(WebPanelBase):
    def test_issue_and_resolve(self):
        raw = webpanel.issue_token(SHOP, USER)
        got = webpanel.resolve_token(raw)
        self.assertEqual(got, {"chat_id": SHOP, "user_id": USER})

    def test_bad_token_rejected(self):
        self.assertIsNone(webpanel.resolve_token("nope"))
        self.assertIsNone(webpanel.resolve_token(""))

    def test_revoke_kills_all_shop_tokens(self):
        raw1 = webpanel.issue_token(SHOP, USER)
        raw2 = webpanel.issue_token(SHOP, USER)
        n = webpanel.revoke_tokens(SHOP)
        self.assertEqual(n, 2)
        self.assertIsNone(webpanel.resolve_token(raw1))
        self.assertIsNone(webpanel.resolve_token(raw2))

    def test_expired_token_rejected(self):
        raw = webpanel.issue_token(SHOP, USER, ttl_hours=-1)
        self.assertIsNone(webpanel.resolve_token(raw))

    def test_tokens_hashed_at_rest(self):
        raw = webpanel.issue_token(SHOP, USER)
        with db.get_db() as conn:
            rows = conn.execute("SELECT token_hash FROM web_tokens").fetchall()
        self.assertNotIn(raw, [r["token_hash"] for r in rows])

    def test_panel_url(self):
        url = webpanel.panel_url("https://x.example.com/", "abc")
        self.assertEqual(url, "https://x.example.com/panel?t=abc")

    def test_tokens_are_markdown_safe(self):
        """'_' or '-' in a token breaks Telegram Markdown → message never sends
        (this silently killed /invitevendor and /webpanel in production)."""
        for _ in range(200):
            for tok in (
                webpanel.issue_token(SHOP, USER),
                webpanel.create_vendor_invite(USER, "note"),
            ):
                self.assertTrue(tok.isalnum(), f"token not markdown-safe: {tok}")
                self.assertNotIn("_", tok)
                self.assertNotIn("-", tok)
                self.assertNotIn("*", tok)


class InviteTests(WebPanelBase):
    def test_invite_single_use(self):
        raw = webpanel.create_vendor_invite(1, "Oliver")
        ok, note = webpanel.redeem_vendor_invite(raw, USER)
        self.assertTrue(ok)
        self.assertEqual(note, "Oliver")
        ok2, msg = webpanel.redeem_vendor_invite(raw, USER + 1)
        self.assertFalse(ok2)
        self.assertIn("already used", msg)

    def test_unknown_invite(self):
        ok, msg = webpanel.redeem_vendor_invite("bogus", USER)
        self.assertFalse(ok)


class ApiTests(WebPanelBase):
    def test_state_shape(self):
        db.add_product(SHOP, "BPC", 41.0, 5)
        db.add_payment_method(SHOP, "Cash App", "$tag")
        code, data = webpanel.api_state(self.tok)
        self.assertEqual(code, 200)
        self.assertEqual(data["shop"]["title"], "Vendor Shop")
        self.assertEqual(len(data["products"]), 1)
        self.assertEqual(len(data["payments"]), 1)

    def test_create_product_and_audit(self):
        code, data = webpanel.api_product(
            self.tok, {"name": "TB-500", "price": "47", "stock": "8"}
        )
        self.assertEqual(code, 200)
        self.assertTrue(data["created"])
        p = db.get_product(data["id"])
        self.assertEqual(p["stock"], 8)
        with db.get_db() as conn:
            audit = conn.execute(
                "SELECT * FROM stock_audit WHERE reason = 'web_panel'"
            ).fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["delta"], 8)

    def test_update_product_stock_and_price(self):
        pid = db.add_product(SHOP, "BPC", 41.0, 5)
        code, _ = webpanel.api_product(
            self.tok, {"id": pid, "price": 45, "stock": 2, "kit_price": 294}
        )
        self.assertEqual(code, 200)
        p = db.get_product(pid)
        self.assertEqual(p["price"], 45.0)
        self.assertEqual(p["stock"], 2)
        self.assertEqual(p["kit_price"], 294.0)

    def test_cannot_touch_other_shops_product(self):
        db.ensure_shop(SHOP + 1, title="Other")
        pid = db.add_product(SHOP + 1, "Foreign", 10.0, 1)
        code, _ = webpanel.api_product(self.tok, {"id": pid, "price": 1})
        self.assertEqual(code, 404)

    def test_product_validation(self):
        code, _ = webpanel.api_product(self.tok, {"name": "X", "price": -5})
        self.assertEqual(code, 400)
        code, _ = webpanel.api_product(self.tok, {"name": "", "price": 5})
        self.assertEqual(code, 400)
        pid = db.add_product(SHOP, "BPC", 41.0, 5)
        code, _ = webpanel.api_product(self.tok, {"id": pid, "stock": -1})
        self.assertEqual(code, 400)

    def test_bulk_import_upserts(self):
        db.add_product(SHOP, "BPC-157 10MG", 41.0, 5)
        code, data = webpanel.api_bulk(
            self.tok, {"text": "BPC-157 10MG | 45 | 9\nTB-500 10MG | 47 | 3\n"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(data["created"], 1)
        self.assertEqual(data["updated"], 1)

    def test_payment_lifecycle(self):
        code, data = webpanel.api_payment(
            self.tok, {"name": "Zelle", "instructions": "send to x"}
        )
        self.assertEqual(code, 200)
        mid = data["id"]
        code, _ = webpanel.api_payment(self.tok, {"id": mid, "active": False})
        self.assertEqual(code, 200)
        self.assertEqual(db.get_payment_method(mid)["active"], 0)
        code, data = webpanel.api_payment(self.tok, {"id": mid, "delete": True})
        self.assertEqual(code, 200)
        self.assertIsNone(db.get_payment_method(mid))

    def test_shipping_and_shop(self):
        code, _ = webpanel.api_shipping(
            self.tok, {"enabled": True, "fee": 9.5, "free_above": 200}
        )
        self.assertEqual(code, 200)
        shop = db.get_shop(SHOP)
        self.assertEqual(shop["shipping_fee"], 9.5)
        code, _ = webpanel.api_shop(self.tok, {"title": "Oliver Peptides"})
        self.assertEqual(code, 200)
        self.assertEqual(db.get_shop(SHOP)["title"], "Oliver Peptides")


class HttpLayerTests(WebPanelBase):
    def test_get_page(self):
        code, ctype, body = webpanel.handle_panel_get("/panel", {})
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"Shop Panel", body)

    def test_state_requires_token(self):
        code, _, _ = webpanel.handle_panel_get("/panel/api/state", {"t": ["bad"]})
        self.assertEqual(code, 401)
        raw = webpanel.issue_token(SHOP, USER)
        code, _, body = webpanel.handle_panel_get("/panel/api/state", {"t": [raw]})
        self.assertEqual(code, 200)
        self.assertIn(b"Vendor Shop", body)

    def test_post_requires_token(self):
        code, _, _ = webpanel.handle_panel_post(
            "/panel/api/product", {"t": "bad", "name": "X", "price": 5}
        )
        self.assertEqual(code, 401)
        raw = webpanel.issue_token(SHOP, USER)
        code, _, _ = webpanel.handle_panel_post(
            "/panel/api/product", {"t": raw, "name": "X", "price": 5}
        )
        self.assertEqual(code, 200)

    def test_unknown_endpoint(self):
        code, _, _ = webpanel.handle_panel_post("/panel/api/nope", {"t": "x"})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
