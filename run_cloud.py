#!/usr/bin/env python3
"""Cloud entrypoint: SPBC notify HTTP API (Render web) + Telegram bot polling.

The HTTP server (spbc_notify.serve_http) answers:
  GET  /  /health        Render health check + status JSON
  POST /notify           spbc-orders worker → owner/supplier Telegram messages
  POST /resolve-chat     username → chat id (admin Suppliers lookup)
  GET  /recent-chats     recent chats picker
"""

from __future__ import annotations

import logging
import os
import threading

import spbc_notify

log = logging.getLogger("run_cloud")


def _bind_vendor_miniapps() -> None:
    """Re-attach claim tokens + issue public storefront keys for vendor shops.

    Claim token (UNICORN_CLAIM_TOKEN / invite) binds vendor_invites for /start
    redeem only. Catalog uses a separate storefront_key returned in the result
    (copy into Cloudflare Pages — never put the claim token in public HTML).
    """
    import webpanel

    # Legacy single-vendor env (Unicorn first). Panel handoff lives on the
    # service that has PANEL_BASE_URL = spbc-supplier-bot (virtual shop 9.1e12).
    claim = (os.getenv("UNICORN_CLAIM_TOKEN") or "").strip()
    shop_raw = (os.getenv("UNICORN_SHOP_CHAT_ID") or "").strip().strip("\"'")
    token_set = bool((os.getenv("UNICORN_BOT_TOKEN") or "").strip())
    print(
        f"[run_cloud] unicorn env: claim={bool(claim)} shop={shop_raw!r} "
        f"bot_token_set={token_set}",
        flush=True,
    )
    if claim:
        try:
            shop_id = int(shop_raw) if shop_raw else None
        except ValueError:
            shop_id = None
        result = webpanel.ensure_miniapp_storefront(
            claim,
            shop_chat_id=shop_id,
            title_hints=[
                "unicorn",
                "magic factory",
                "unicorn magic",
                "unicorn fancy",
                "@unicornmagicfactory",
            ],
            note="@unicornmagicfactory",
        )
        log.info("unicorn storefront bind: %s", result)
        print(f"[run_cloud] unicorn storefront bind: {result}", flush=True)
        if result.get("storefront_key"):
            print(
                f"[run_cloud] unicorn PUBLIC storefront_key (Pages only): "
                f"{result['storefront_key']}",
                flush=True,
            )
        # Seed payment rails she can edit anytime in her panel link
        sid = result.get("shop_chat_id") or shop_id
        if sid:
            pay = webpanel.ensure_shop_payments(
                int(sid),
                [
                    {"method_type": "venmo", "handle": "@wineboos"},
                    {
                        "method_type": "paypal",
                        "handle": "unicornfartzz@proton.me",
                        "network_note": "friends_family",
                    },
                ],
            )
            log.info("unicorn payments seed: %s", pay)
            print(f"[run_cloud] unicorn payments seed: {pay}", flush=True)

    # Optional multi-vendor JSON: each entry may include invite + shop_chat_id + name
    raw = (os.getenv("VENDOR_STORES_JSON") or "").strip()
    if not raw:
        return
    try:
        import json

        vendors = json.loads(raw)
    except Exception:
        log.exception("VENDOR_STORES_JSON invalid — skip miniapp bind")
        return
    if not isinstance(vendors, list):
        return
    for v in vendors:
        if not isinstance(v, dict):
            continue
        invite = (v.get("invite") or "").strip()
        if not invite:
            continue
        sid = v.get("shop_chat_id")
        try:
            sid_i = int(sid) if sid not in (None, "") else None
        except (TypeError, ValueError):
            sid_i = None
        name = (v.get("name") or "").strip()
        hints = [name] if name else []
        result = webpanel.ensure_miniapp_storefront(
            invite,
            shop_chat_id=sid_i,
            title_hints=hints,
            note=name,
        )
        log.info("vendor storefront bind (%s): %s", name or invite[:12], result)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(
        target=spbc_notify.serve_http, args=(port,), name="notify-http", daemon=True
    ).start()

    # Bind mini-app invite → stocked shop BEFORE vendor receivers start polling
    try:
        _bind_vendor_miniapps()
    except Exception:
        log.exception("miniapp storefront bind failed (continuing boot)")

    # Vendor mini-app order receivers (one branded bot per vendor, all sharing
    # this process and database). Configured via VENDOR_STORES_JSON, with the
    # legacy UNICORN_* vars still honored. No vendors configured = no threads.
    import vendor_stores

    vendor_stores.start_all()

    # Weekly vendor service-fee autobiller (reads orders + writes invoices only).
    # Isolated daemon — must never block bot startup or order handling.
    try:
        import autobiller

        autobiller.start_autobiller(daemon=True)
    except Exception:
        log.exception("autobiller start failed (continuing boot)")

    try:
        import reservation_janitor

        reservation_janitor.start_reservation_janitor(daemon=True)
    except Exception:
        log.exception("reservation janitor start failed (continuing boot)")

    import bot

    bot.main()


if __name__ == "__main__":
    main()
