#!/usr/bin/env python3
"""Cloud entrypoint: SPBC notify HTTP API (Render web) + Telegram bot polling.

The HTTP server (spbc_notify.serve_http) answers:
  GET  /  /health        Render health check + status JSON
  POST /notify           spbc-orders worker → owner/supplier Telegram messages
  POST /resolve-chat     username → chat id (admin Suppliers lookup)
  GET  /recent-chats     recent chats picker
"""

from __future__ import annotations

import os
import threading

import spbc_notify


def main() -> None:
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(
        target=spbc_notify.serve_http, args=(port,), name="notify-http", daemon=True
    ).start()

    # Optional: vendor mini-app order receiver (@UnicornMagicFactoryBot).
    # Only starts when UNICORN_BOT_TOKEN is set; a crash logs and never takes
    # down the main bot.
    if (os.environ.get("UNICORN_BOT_TOKEN") or "").strip():
        import unicorn_store_bot

        threading.Thread(
            target=unicorn_store_bot.run_threaded, name="unicorn-store", daemon=True
        ).start()

    import bot

    bot.main()


if __name__ == "__main__":
    main()
