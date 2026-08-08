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


JPEG = b"\xff\xd8\xff\xe0" + b"x" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40
PDF = b"%PDF-1.4\n" + b"x" * 40


def data_url(mime, blob):
    import base64

    return f"data:{mime};base64," + base64.b64encode(blob).decode()


class UploadTests(WebPanelBase):
    def setUp(self):
        super().setUp()
        self._media = tempfile.TemporaryDirectory()
        self._p1 = mock.patch.object(webpanel, "MEDIA_DIR", self._media.name)
        self._p2 = mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        )
        self._p1.start()
        self._p2.start()
        webpanel._upload_budget.clear()
        self.pid = db.add_product(SHOP, "BPC", 41.0, 5)

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self._media.cleanup()
        super().tearDown()

    def test_photo_upload_sets_url(self):
        code, data = webpanel.api_media(
            self.tok, {"id": self.pid, "kind": "photo", "data_url": data_url("image/jpeg", JPEG)}
        )
        self.assertEqual(code, 200, data)
        url = data["photo_url"]
        self.assertTrue(url.startswith("https://bot.example.com/media/"))
        self.assertEqual(db.get_product(self.pid)["photo_file_id"], url)
        # served back with the right type
        got = webpanel.read_media(url.rsplit("/", 1)[-1])
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "image/jpeg")

    def test_declared_mime_cannot_smuggle_a_type(self):
        """An HTML payload labelled image/png must be refused (magic wins)."""
        evil = b"<html><script>alert(1)</script></html>"
        code, data = webpanel.api_media(
            self.tok,
            {"id": self.pid, "kind": "photo", "data_url": data_url("image/png", evil)},
        )
        self.assertEqual(code, 400)
        self.assertIn("JPG, PNG or PDF", data["error"])

    def test_pdf_rejected_as_product_photo(self):
        code, _ = webpanel.api_media(
            self.tok,
            {"id": self.pid, "kind": "photo", "data_url": data_url("application/pdf", PDF)},
        )
        self.assertEqual(code, 400)

    def test_coa_pdf_and_link(self):
        code, data = webpanel.api_media(
            self.tok,
            {"id": self.pid, "kind": "coa", "data_url": data_url("application/pdf", PDF)},
        )
        self.assertEqual(code, 200, data)
        self.assertTrue(db.get_product(self.pid)["coa_url"].endswith(".pdf"))
        code2, data2 = webpanel.api_media(
            self.tok, {"id": self.pid, "kind": "coa", "url": "https://lab.example.com/a.pdf"}
        )
        self.assertEqual(code2, 200, data2)
        self.assertEqual(
            db.get_product(self.pid)["coa_url"], "https://lab.example.com/a.pdf"
        )
        code3, _ = webpanel.api_media(
            self.tok, {"id": self.pid, "kind": "coa", "url": "javascript:alert(1)"}
        )
        self.assertEqual(code3, 400)

    def test_clear_photo_and_coa(self):
        webpanel.api_media(
            self.tok, {"id": self.pid, "kind": "photo", "data_url": data_url("image/png", PNG)}
        )
        webpanel.api_media(self.tok, {"id": self.pid, "kind": "photo", "clear": True})
        self.assertIsNone(db.get_product(self.pid)["photo_file_id"])

    def test_cannot_upload_to_another_shop(self):
        db.ensure_shop(SHOP + 1, title="Other")
        other = db.add_product(SHOP + 1, "Foreign", 10.0, 1)
        code, _ = webpanel.api_media(
            self.tok, {"id": other, "kind": "photo", "data_url": data_url("image/png", PNG)}
        )
        self.assertEqual(code, 404)

    def test_media_name_validation_blocks_traversal(self):
        for bad in (
            "../../etc/passwd",
            "..%2f..%2fetc",
            "abc.jpg",
            "0123456789abcdef0123456789abcdef.exe",
            "",
        ):
            self.assertIsNone(webpanel.read_media(bad), bad)

    def test_daily_budget_enforced(self):
        webpanel._upload_budget[SHOP] = {
            "day": webpanel._utc_now().strftime("%Y-%m-%d"),
            "count": webpanel.UPLOADS_PER_DAY,
            "bytes": 0,
        }
        code, data = webpanel.api_media(
            self.tok, {"id": self.pid, "kind": "photo", "data_url": data_url("image/png", PNG)}
        )
        self.assertEqual(code, 429)


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
