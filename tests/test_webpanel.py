"""Vendor web panel: tokens, invites, and the JSON API."""

from __future__ import annotations

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
        ok, note, prebuilt = webpanel.redeem_vendor_invite(raw, USER)
        self.assertTrue(ok)
        self.assertEqual(note, "Oliver")
        self.assertIsNone(prebuilt)
        ok2, msg, _ = webpanel.redeem_vendor_invite(raw, USER + 1)
        self.assertFalse(ok2)
        self.assertIn("already used", msg)

    def test_unknown_invite(self):
        ok, msg, _ = webpanel.redeem_vendor_invite("bogus", USER)
        self.assertFalse(ok)

    def test_redeem_both_deep_link_payload_forms(self):
        """Regression: field links use vendor<hex24> (no underscore); /handover
        used vendor_<hex24>. Both must normalize and redeem."""
        raw_a = webpanel.create_vendor_invite(1, "NoUnderscore")
        self.assertEqual(len(raw_a), 24)
        self.assertTrue(all(c in "0123456789abcdef" for c in raw_a))

        # Markdown-safe form (what create_vendor_invite field links use)
        payload_a = f"vendor{raw_a}"
        self.assertEqual(webpanel.normalize_invite_token(payload_a), raw_a)
        ok, note, prebuilt = webpanel.redeem_vendor_invite(payload_a, USER)
        self.assertTrue(ok, note)
        self.assertEqual(note, "NoUnderscore")
        self.assertIsNone(prebuilt)

        # Legacy underscore form
        raw_b = webpanel.create_vendor_invite(1, "WithUnderscore")
        payload_b = f"vendor_{raw_b}"
        self.assertEqual(webpanel.normalize_invite_token(payload_b), raw_b)
        ok, note, prebuilt = webpanel.redeem_vendor_invite(payload_b, USER + 1)
        self.assertTrue(ok, note)
        self.assertEqual(note, "WithUnderscore")
        self.assertIsNone(prebuilt)

        # Bare hex still works (normalize is a no-op)
        raw_c = webpanel.create_vendor_invite(1, "Bare")
        self.assertEqual(webpanel.normalize_invite_token(raw_c), raw_c)
        ok, note, _ = webpanel.redeem_vendor_invite(raw_c, USER + 2)
        self.assertTrue(ok, note)

        # Handler gate: remainder after normalize must be 24 hex — not other
        # start payloads that merely begin with the letters "vendor"
        self.assertNotEqual(
            len(webpanel.normalize_invite_token("vendor_nothex")), 24
        )
        self.assertEqual(
            webpanel.normalize_invite_token("shop_abc"), "shop_abc"
        )

    def test_handover_invite_carries_prebuilt_shop(self):
        shop = db.create_virtual_shop("Prebuilt Vendor", created_by=1)
        sid = int(shop["chat_id"])
        self.assertTrue(db.is_virtual_shop(sid))
        raw = webpanel.create_vendor_invite(1, "Prebuilt Vendor", shop_chat_id=sid)
        ok, note, prebuilt = webpanel.redeem_vendor_invite(raw, USER)
        self.assertTrue(ok)
        self.assertEqual(prebuilt, sid)

    def test_virtual_shop_ids_do_not_collide(self):
        a = int(db.create_virtual_shop("A", 1)["chat_id"])
        b = int(db.create_virtual_shop("B", 1)["chat_id"])
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(a, db.VIRTUAL_SHOP_BASE)
        # a real Telegram user/group id is never mistaken for a virtual shop
        self.assertFalse(db.is_virtual_shop(6086230967))
        self.assertFalse(db.is_virtual_shop(-1001234567890))

    def test_ensure_miniapp_storefront_binds_invite_without_claim(self):
        shop = db.create_virtual_shop("Unicorn Magic Factory", created_by=1)
        sid = int(shop["chat_id"])
        db.add_product(sid, "SEMA 5mg", 55.0, 12)
        fixed = "3a9eee77166edc67b4cbb94d"
        result = webpanel.ensure_miniapp_storefront(
            f"vendor{fixed}",
            title_hints=["unicorn", "magic factory"],
            note="Unicorn Magic Factory",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["shop_chat_id"], sid)
        self.assertGreaterEqual(result["products"], 1)
        sf_key = result["storefront_key"]
        self.assertTrue(sf_key)
        self.assertNotEqual(sf_key, fixed)
        # Catalog is keyed by storefront_key, not the claim token
        code, body = webpanel.api_storefront(f"vendor{sf_key}")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["shop"]["title"], "Unicorn Magic Factory")
        self.assertEqual(len(body["products"]), 1)
        # Claim token must NOT open the public catalog
        code_claim, body_claim = webpanel.api_storefront(f"vendor{fixed}")
        self.assertEqual(code_claim, 404, body_claim)
        # Idempotent bind + same storefront key
        again = webpanel.ensure_miniapp_storefront(f"vendor{fixed}")
        self.assertTrue(again["ok"])
        self.assertEqual(again["action"], "already_bound")
        self.assertEqual(again["storefront_key"], sf_key)

    def test_ensure_miniapp_does_not_steal_unrelated_stocked_shop(self):
        # Large main catalog should not become Unicorn's mini-app just because it's biggest
        main = db.ensure_shop(-100111, title="Shop")
        db.add_product(int(main["chat_id"]), "Main SEMA", 50.0, 99)
        empty = db.create_virtual_shop("Unicorn Magic Factory", created_by=1)
        fixed = "aaaaaaaaaaaaaaaaaaaaaaaa"
        result = webpanel.ensure_miniapp_storefront(
            f"vendor{fixed}",
            title_hints=["unicorn", "magic factory"],
            note="Unicorn Magic Factory",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["shop_chat_id"], int(empty["chat_id"]))
        self.assertEqual(result["products"], 0)
        code, body = webpanel.api_storefront(result["storefront_key"])
        self.assertEqual(code, 200, body)
        self.assertEqual(body["products"], [])

    def test_storefront_key_cannot_be_redeemed_as_claim(self):
        """CRIT1: public catalog key must never grant shop admin."""
        shop = db.create_virtual_shop("Unicorn Magic Factory", created_by=1)
        sid = int(shop["chat_id"])
        db.add_product(sid, "SEMA 5mg", 55.0, 12)
        claim = "bbbbbbbbbbbbbbbbbbbbbbbb"
        result = webpanel.ensure_miniapp_storefront(
            f"vendor{claim}",
            title_hints=["unicorn"],
            note="Unicorn Magic Factory",
        )
        self.assertTrue(result["ok"], result)
        sf_key = result["storefront_key"]
        self.assertNotEqual(sf_key, claim)
        # Storefront key is not a vendor_invites row → redeem fails
        ok, msg, pre = webpanel.redeem_vendor_invite(sf_key, USER)
        self.assertFalse(ok)
        self.assertIsNone(pre)
        self.assertIn("not found", msg.lower())
        # Real claim token still redeems
        ok2, note, pre2 = webpanel.redeem_vendor_invite(f"vendor{claim}", USER)
        self.assertTrue(ok2, note)
        self.assertEqual(pre2, sid)

    def test_claim_token_rejected_by_api_storefront(self):
        """CRIT1: claim credential must not serve as catalog key."""
        shop = db.create_virtual_shop("Unicorn Magic Factory", created_by=1)
        db.add_product(int(shop["chat_id"]), "SEMA 5mg", 55.0, 1)
        claim = "cccccccccccccccccccccccc"
        result = webpanel.ensure_miniapp_storefront(
            claim, title_hints=["unicorn"]
        )
        self.assertTrue(result["ok"], result)
        code, body = webpanel.api_storefront(claim)
        self.assertEqual(code, 404, body)
        code2, body2 = webpanel.api_storefront(result["storefront_key"])
        self.assertEqual(code2, 200, body2)

    def test_bind_refuses_two_virtual_shops_with_no_hint_match(self):
        """CRIT2: never pick a wrong vendor when hints match neither shop."""
        a = db.create_virtual_shop("Alpha Peptides", created_by=1)
        b = db.create_virtual_shop("Beta Research", created_by=1)
        db.add_product(int(a["chat_id"]), "A", 10.0, 5)
        db.add_product(int(b["chat_id"]), "B", 20.0, 5)
        fixed = "dddddddddddddddddddddddd"
        result = webpanel.ensure_miniapp_storefront(
            f"vendor{fixed}",
            title_hints=["unicorn", "magic factory"],
            note="Unicorn",
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("error"), "no_shop_found")

    def test_bind_hint_matches_exactly_one_of_two_virtual_shops(self):
        """CRIT2: title hint selects the correct shop among several virtuals."""
        a = db.create_virtual_shop("Alpha Peptides", created_by=1)
        b = db.create_virtual_shop("Unicorn Magic Factory", created_by=1)
        db.add_product(int(a["chat_id"]), "A", 10.0, 50)
        db.add_product(int(b["chat_id"]), "U", 55.0, 3)
        fixed = "eeeeeeeeeeeeeeeeeeeeeeee"
        result = webpanel.ensure_miniapp_storefront(
            f"vendor{fixed}",
            title_hints=["unicorn", "magic factory"],
            note="Unicorn",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["shop_chat_id"], int(b["chat_id"]))

    def test_explicit_shop_chat_id_miss_does_not_guess(self):
        """CRIT2: missing explicit shop id → no-shop, not heuristic fallthrough."""
        db.create_virtual_shop("Unicorn Magic Factory", created_by=1)
        fixed = "ffffffffffffffffffffffff"
        result = webpanel.ensure_miniapp_storefront(
            f"vendor{fixed}",
            shop_chat_id=999_999_999_999,  # does not exist
            title_hints=["unicorn"],
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("error"), "no_shop_found")

    def test_rebind_rejects_wrong_virtual_bind_when_hints_given(self):
        """CRIT2: wrong prior virtual bind must not stick when title hints fail."""
        wrong = db.create_virtual_shop("Other Vendor Stocked", created_by=1)
        right = db.create_virtual_shop("Unicorn Magic Factory", created_by=1)
        db.add_product(int(wrong["chat_id"]), "X", 1.0, 9)
        db.add_product(int(right["chat_id"]), "U", 55.0, 3)
        claim = "111111111111111111111111"
        # Force a bad prior bind to the wrong stocked virtual shop
        webpanel.ensure_webpanel_tables()
        now = webpanel._utc_now()
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO vendor_invites (token_hash, note, created_by, "
                "created_at, expires_at, shop_chat_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    webpanel._hash(claim),
                    "bad",
                    0,
                    webpanel._ts(now),
                    webpanel._ts(now + timedelta(days=3650)),
                    int(wrong["chat_id"]),
                ),
            )
        result = webpanel.ensure_miniapp_storefront(
            claim,
            title_hints=["unicorn", "magic factory"],
            note="Unicorn",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["shop_chat_id"], int(right["chat_id"]))
        self.assertNotEqual(result["shop_chat_id"], int(wrong["chat_id"]))


class RestockTests(WebPanelBase):
    def test_restock_adds_and_audits(self):
        p1 = db.add_product(SHOP, "BPC", 41.0, 5)
        p2 = db.add_product(SHOP, "TB-500", 47.0, 0)
        code, data = webpanel.api_restock(
            self.tok, {"items": [{"id": p1, "add": 10}, {"id": p2, "add": 3}]}
        )
        self.assertEqual(code, 200, data)
        self.assertEqual(data["count"], 2)
        self.assertEqual(db.get_product(p1)["stock"], 15)  # added, not replaced
        self.assertEqual(db.get_product(p2)["stock"], 3)
        with db.get_db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM stock_audit WHERE reason='web_panel'"
            ).fetchone()["c"]
        self.assertEqual(n, 2)

    def test_restock_rejects_bad_and_foreign(self):
        db.ensure_shop(SHOP + 1, title="Other")
        foreign = db.add_product(SHOP + 1, "Foreign", 10.0, 1)
        mine = db.add_product(SHOP, "BPC", 41.0, 5)
        code, data = webpanel.api_restock(
            self.tok,
            {"items": [{"id": foreign, "add": 5}, {"id": mine, "add": -3}]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(db.get_product(foreign)["stock"], 1)
        self.assertEqual(db.get_product(mine)["stock"], 5)

    def test_restock_empty(self):
        code, _ = webpanel.api_restock(self.tok, {"items": []})
        self.assertEqual(code, 400)


class ApiTests(WebPanelBase):
    def test_state_shape(self):
        db.add_product(SHOP, "BPC", 41.0, 5)
        db.add_payment_method(SHOP, "Cash App", "$tag")
        code, data = webpanel.api_state(self.tok)
        self.assertEqual(code, 200)
        self.assertEqual(data["shop"]["title"], "Vendor Shop")
        self.assertEqual(len(data["products"]), 1)
        self.assertEqual(len(data["payments"]), 1)

    def test_payment_typed_create_update_and_seed(self):
        code, data = webpanel.api_payment(
            self.tok, {"method_type": "venmo", "handle": "@wineboos"}
        )
        self.assertEqual(code, 200, data)
        mid = data["id"]
        methods = db.list_payment_methods(SHOP, active_only=False)
        self.assertEqual(methods[0]["method_type"], "venmo")
        self.assertIn("@wineboos", methods[0]["handle"])
        # update handle anytime
        code, data = webpanel.api_payment(
            self.tok,
            {"id": mid, "method_type": "venmo", "handle": "@newhandle", "active": True},
        )
        self.assertEqual(code, 200, data)
        m = db.get_payment_method(mid)
        self.assertEqual(m["handle"], "@newhandle")
        # seed is idempotent for existing types; adds paypal
        seed = webpanel.ensure_shop_payments(
            SHOP,
            [
                {"method_type": "venmo", "handle": "@ignored"},
                {"method_type": "paypal", "handle": "unicornfartzz@proton.me"},
                {"method_type": "zelle", "handle": "555-0100"},
            ],
        )
        self.assertIn("paypal", seed["created"])
        self.assertIn("zelle", seed["created"])
        self.assertNotIn("venmo", seed["created"])
        types = {m["method_type"] for m in db.list_payment_methods(SHOP, active_only=False)}
        self.assertEqual(types, {"venmo", "paypal", "zelle"})

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
