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

    # Vendor mini-app order receivers (one branded bot per vendor, all sharing
    # this process and database). Configured via VENDOR_STORES_JSON, with the
    # legacy UNICORN_* vars still honored. No vendors configured = no threads.
    import vendor_stores

    vendor_stores.start_all()

    import bot

    bot.main()


if __name__ == "__main__":
    main()
