"""Rejected or missing SPBC token must not exit the Render web process.

spbc-supplier-bot exited 1 on getMe 401 (2026-09-10) until stuck_crashlooping.
Scratch / mocks only. Does not open inventory.db and does not call Telegram.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import run_cloud  # noqa: E402


class _Wait:
    def __init__(self, waited: list[bool]) -> None:
        self._waited = waited

    def wait(self) -> None:
        self._waited.append(True)


class TokenConfigTests(unittest.TestCase):
    def test_invalid_token_message_has_fingerprint_not_secret(self) -> None:
        class InvalidToken(Exception):
            pass

        exc = InvalidToken("Unauthorized")
        self.assertTrue(bot.token_config_failure(exc))
        text = bot.token_rejected_message("8583721412:A3G8", exc)
        self.assertIn("8583721412:A3G8", text)
        self.assertIn("401", text)
        self.assertIn("TELEGRAM_BOT_TOKEN", text)
        self.assertNotIn("123456:ABC", text)

    def test_conflict_is_not_a_missing_config_failure(self) -> None:
        class Conflict(Exception):
            pass

        self.assertFalse(
            bot.token_config_failure(Conflict("terminated by other getUpdates request"))
        )


class ForegroundBootTests(unittest.TestCase):
    def test_missing_token_stays_up(self) -> None:
        waited: list[bool] = []
        with mock.patch("config.resolve_bot_tokens", return_value=[]), mock.patch.object(
            run_cloud.threading, "Event", lambda: _Wait(waited)
        ):
            run_cloud._run_foreground()
        self.assertEqual(waited, [True])

    def test_rejected_token_stays_up(self) -> None:
        class InvalidToken(Exception):
            pass

        waited: list[bool] = []

        def boom() -> None:
            raise InvalidToken("Unauthorized")

        with mock.patch(
            "config.resolve_bot_tokens", return_value=["123456:ABC-secret"]
        ), mock.patch.object(
            run_cloud.threading, "Event", lambda: _Wait(waited)
        ), mock.patch("bot.main", boom):
            run_cloud._run_foreground()
        self.assertEqual(waited, [True])

    def test_main_returns_token_rejected_instead_of_exiting(self) -> None:
        class InvalidToken(Exception):
            pass

        app = mock.Mock()
        app.run_polling.side_effect = InvalidToken("Unauthorized")
        app.bot_data = {}
        with mock.patch.object(bot, "setup_logging"), mock.patch.object(
            bot, "_acquire_single_instance_lock", return_value=None
        ), mock.patch.object(
            bot, "resolve_bot_tokens", return_value=["123456:ABC-secret"]
        ), mock.patch.object(
            bot.token_pool, "resolve_active_index", return_value=0
        ), mock.patch.object(
            bot.token_pool, "load_state", return_value={"dead_tokens": []}
        ), mock.patch.object(
            bot.token_pool, "save_state"
        ), mock.patch.object(
            bot.token_pool, "token_fingerprint", return_value="123456:CRET"
        ), mock.patch.object(bot, "build_app", return_value=app), mock.patch.object(
            bot, "TOKEN_FAILOVER", False
        ):
            self.assertEqual(bot.main(), "token_rejected")
        app.run_polling.assert_called_once()


class SkipPollingTests(unittest.TestCase):
    def test_skip_flag_does_not_start_telegram_pollers(self) -> None:
        waited: list[bool] = []
        threads: list[str] = []

        class FakeThread:
            def __init__(self, *args, **kwargs) -> None:
                threads.append(str(kwargs.get("name")))

            def start(self) -> None:
                return None

        with mock.patch.dict(os.environ, {"SKIP_BOT_POLLING": "1"}), mock.patch.object(
            run_cloud, "_bind_vendor_miniapps"
        ), mock.patch(
            "vendor_stores.start_all", side_effect=AssertionError("vendor poller")
        ), mock.patch(
            "autobiller.start_autobiller", side_effect=AssertionError("autobiller")
        ), mock.patch.object(
            run_cloud.threading, "Thread", FakeThread
        ), mock.patch.object(
            run_cloud.threading, "Event", lambda: _Wait(waited)
        ):
            self.assertTrue(run_cloud.skip_bot_polling())
            run_cloud.main()
        self.assertEqual(threads, ["notify-http"])
        self.assertEqual(waited, [True])


if __name__ == "__main__":
    unittest.main()
