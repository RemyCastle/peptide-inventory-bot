"""Vendor storefront bot: cache-busted Mini App URL + /webpanel weblink."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import vendor_stores  # noqa: E402
import webpanel  # noqa: E402

SHOP = 96001
OWNER = 73001
ADMIN = 73002
NOTIFY = 73003
STRANGER = 73999


def _run(coro):
    return asyncio.run(coro)


class CacheBustStoreUrlTests(unittest.TestCase):
    def test_appends_v_query(self) -> None:
        url = vendor_stores.cache_bust_store_url(
            "https://remy-miniapp-demos.pages.dev/unicorn/"
        )
        self.assertEqual(
            url,
            "https://remy-miniapp-demos.pages.dev/unicorn/?v=20260828",
        )

    def test_replaces_existing_v(self) -> None:
        url = vendor_stores.cache_bust_store_url(
            "https://example.com/shop/?v=old&invite=abc"
        )
        self.assertIn("v=20260828", url)
        self.assertNotIn("v=old", url)
        self.assertIn("invite=abc", url)

    def test_empty_unchanged(self) -> None:
        self.assertEqual(vendor_stores.cache_bust_store_url(""), "")
        self.assertEqual(vendor_stores.cache_bust_store_url("   "), "")

    def test_legacy_default_store_url_is_cache_busted(self) -> None:
        env = {
            "UNICORN_BOT_TOKEN": "123456:TESTTOKEN",
            "UNICORN_STORE_URL": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            vendors = vendor_stores.load_vendor_configs()
        unicorn = next(v for v in vendors if v.get("token") == "123456:TESTTOKEN")
        self.assertEqual(
            unicorn["store_url"],
            "https://remy-miniapp-demos.pages.dev/unicorn/?v=20260828",
        )


class VendorWebpanelAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "vendor_panel.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Unicorn Test Shop")
        db.add_admin(SHOP, ADMIN, "ghostie", OWNER)
        webpanel.ensure_webpanel_tables()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_admin_notify_owner_allowed_stranger_denied(self) -> None:
        notify = [NOTIFY]
        self.assertTrue(
            vendor_stores.can_access_vendor_webpanel(ADMIN, SHOP, notify)
        )
        self.assertTrue(
            vendor_stores.can_access_vendor_webpanel(NOTIFY, SHOP, notify)
        )
        with mock.patch.object(db, "is_owner", return_value=True):
            self.assertTrue(
                vendor_stores.can_access_vendor_webpanel(OWNER, SHOP, notify)
            )
        with mock.patch.object(db, "is_owner", return_value=False):
            self.assertFalse(
                vendor_stores.can_access_vendor_webpanel(STRANGER, SHOP, notify)
            )

    def test_mint_reply_has_link_and_bookmark_line(self) -> None:
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            text = vendor_stores.mint_vendor_webpanel_reply(SHOP, ADMIN)
        self.assertIn("https://bot.example.com/panel?t=", text)
        self.assertIn("Bookmark this; send /webpanel for a fresh one.", text)
        raw = text.split("t=", 1)[1].split()[0]
        self.assertEqual(
            webpanel.resolve_token(raw), {"chat_id": SHOP, "user_id": ADMIN}
        )

    def test_mint_unset_base_url(self) -> None:
        with mock.patch.object(webpanel, "PANEL_BASE_URL", ""):
            text = vendor_stores.mint_vendor_webpanel_reply(SHOP, ADMIN)
        self.assertIn("PANEL_BASE_URL unset", text)
        self.assertNotIn("?t=", text)

    def test_revoke_kills_outstanding_links(self) -> None:
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            text = vendor_stores.mint_vendor_webpanel_reply(SHOP, ADMIN)
            raw = text.split("t=", 1)[1].split()[0]
            self.assertIsNotNone(webpanel.resolve_token(raw))
            revoke = vendor_stores.revoke_vendor_webpanel_reply(SHOP)
        self.assertIn("Revoked 1 panel link", revoke)
        self.assertIn("Send /webpanel for a fresh one.", revoke)
        self.assertIsNone(webpanel.resolve_token(raw))


class VendorWebpanelCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self._tmp.name) / "vendor_cmd.db")
        db.init_db()
        db.ensure_shop(SHOP, title="Unicorn Cmd Shop")
        db.add_admin(SHOP, ADMIN, "ghostie", OWNER)
        webpanel.ensure_webpanel_tables()
        self.replies: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _msg(self):
        async def reply_text(text, **_kwargs):
            self.replies.append(text)

        return SimpleNamespace(reply_text=reply_text)

    def _update(self, user_id: int, chat_type: str = "private"):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=user_id, type=chat_type),
            message=self._msg(),
        )

    def _context(self, args: list[str] | None = None):
        return SimpleNamespace(args=args or [], bot=SimpleNamespace())

    def _cmd(self, notify_ids: list[int] | None = None):
        """Build the vendor cmd_webpanel closure the same way _build_app does."""
        shop_chat_id = SHOP
        ids = list(notify_ids or [])

        async def cmd_webpanel(update, context):
            user = update.effective_user
            msg = update.message
            if not vendor_stores.can_access_vendor_webpanel(
                user.id, shop_chat_id, ids
            ):
                await msg.reply_text("Admins only.")
                return
            args = list(getattr(context, "args", None) or [])
            if args and str(args[0]).lower() == "revoke":
                await msg.reply_text(
                    vendor_stores.revoke_vendor_webpanel_reply(shop_chat_id)
                )
                return
            await msg.reply_text(
                vendor_stores.mint_vendor_webpanel_reply(shop_chat_id, user.id)
            )

        return cmd_webpanel

    def test_stranger_rejected(self) -> None:
        _run(self._cmd([NOTIFY])(self._update(STRANGER), self._context()))
        self.assertEqual(self.replies, ["Admins only."])

    def test_admin_mints_link(self) -> None:
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            _run(self._cmd()(self._update(ADMIN), self._context()))
        self.assertEqual(len(self.replies), 1)
        self.assertIn("/panel?t=", self.replies[0])
        self.assertIn("Bookmark this; send /webpanel for a fresh one.", self.replies[0])

    def test_notify_id_can_mint(self) -> None:
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            _run(self._cmd([NOTIFY])(self._update(NOTIFY), self._context()))
        self.assertEqual(len(self.replies), 1)
        self.assertIn("/panel?t=", self.replies[0])

    def test_revoke_arg(self) -> None:
        with mock.patch.object(
            webpanel, "PANEL_BASE_URL", "https://bot.example.com"
        ):
            _run(self._cmd()(self._update(ADMIN), self._context()))
            _run(self._cmd()(self._update(ADMIN), self._context(["revoke"])))
        self.assertEqual(len(self.replies), 2)
        self.assertIn("Revoked", self.replies[1])


class VendorBotHandlerRegistrationTests(unittest.TestCase):
    def test_webpanel_registered_without_catalog_admin_buttons(self) -> None:
        src = inspect.getsource(vendor_stores._build_app)
        self.assertIn('CommandHandler("webpanel", cmd_webpanel)', src)
        self.assertIn('CommandHandler("start", cmd_start)', src)
        self.assertIn('CommandHandler("myid", cmd_myid)', src)
        self.assertIn("cache_bust_store_url", src)
        self.assertIn("WebAppInfo(url=store_url)", src)
        # SPBC back-room callbacks stay
        self.assertIn("voffer_(ok|no)", src)
        self.assertIn("shand_(ok|no)", src)
        # Do not port main-bot Telegram catalog admin onto the vendor bot
        self.assertNotIn('CommandHandler("catalog"', src)
        self.assertNotIn('CommandHandler("admin"', src)
        self.assertNotIn('CommandHandler("addproduct"', src)
        self.assertNotIn('CommandHandler("import"', src)


if __name__ == "__main__":
    unittest.main()
