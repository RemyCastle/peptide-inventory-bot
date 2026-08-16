"""Telethon user-session watcher: passive listener on whitelisted supplier chats.

Run:  python -m supplier_watch.watcher   (from the peptide_inventory_bot dir)

First run prompts for your phone + the Telegram login code, then the session
file keeps you signed in. This process is the ONLY user of that session file
and the ONLY writer of supplier_watch.db.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from . import config, db, parser

# Windows consoles default to cp1252 — emoji in supplier names/alerts must not crash logging
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("supplier_watch")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(config.BASE_DIR / "watcher.log", encoding="utf-8")],
)


def load_suppliers() -> dict[int, str]:
    """suppliers.json → {chat_id: display_name}."""
    if not config.SUPPLIERS_PATH.exists():
        sys.exit(f"Missing {config.SUPPLIERS_PATH} — run: python -m supplier_watch.list_chats")
    raw = json.loads(config.SUPPLIERS_PATH.read_text(encoding="utf-8"))
    chats = {int(c["chat_id"]): str(c.get("name") or c["chat_id"]) for c in raw["chats"]}
    if not chats:
        sys.exit("suppliers.json has no chats — add your supplier chat_ids first.")
    return chats


def format_alerts(supplier: str, alerts: list[dict]) -> str:
    lines = [f"📦 {supplier}"]
    for a in alerts:
        it = a["item"]
        size = f" {it['size']}" if it.get("size") else ""
        cur = "$" if it.get("currency") == "USD" else f"{it.get('currency', '')} "
        if a["kind"] == "new":
            lines.append(f"🆕 {it['product']}{size} — {cur}{it['price']:g}")
        else:
            old, new = a["old_price"], it["price"]
            pct = (new - old) / old * 100 if old else 0
            arrow = "📉" if new < old else "📈"
            lines.append(f"{arrow} {it['product']}{size} — "
                         f"{cur}{old:g} → {cur}{new:g} ({pct:+.1f}%)")
    return "\n".join(lines)


async def main() -> None:
    if not config.TG_API_ID or not config.TG_API_HASH:
        sys.exit("Set TG_API_ID and TG_API_HASH in supplier_watch/.env "
                 "(get them at https://my.telegram.org → API development tools).")

    suppliers = load_suppliers()
    conn = db.connect(config.DB_PATH)
    client = TelegramClient(str(config.SESSION_PATH), config.TG_API_ID, config.TG_API_HASH)

    @client.on(events.NewMessage(chats=list(suppliers)))
    async def on_message(event) -> None:
        text = event.raw_text or ""
        if not any(c.isdigit() for c in text):
            return  # chatter — nothing priced
        chat_id = event.chat_id
        supplier = suppliers.get(chat_id, str(chat_id))
        raw_id = db.save_raw(conn, chat_id, supplier, event.id,
                             event.date.isoformat(), text)
        if raw_id is None:
            return  # already processed (dupe/edit replay)

        # CPU inference takes a while — keep the event loop free
        items, status = await asyncio.to_thread(parser.parse_message, text)
        db.set_parse_status(conn, raw_id, status)
        log.info("chat=%s msg=%s parse=%s items=%d", supplier, event.id, status, len(items))
        if status == "failed" or not items:
            return  # never diff/alert off a failed or empty parse

        alerts = []
        for item in items:
            a = db.record_price(conn, chat_id, supplier, item, raw_id)
            if a is None:
                continue
            if a["kind"] == "change" and config.ALERT_MIN_CHANGE_PCT > 0:
                old = a["old_price"]
                if old and abs(item["price"] - old) / old * 100 < config.ALERT_MIN_CHANGE_PCT:
                    continue
            alerts.append(a)

        if alerts:
            msg = format_alerts(supplier, alerts)
            try:
                await client.send_message("me", msg)
            except FloodWaitError as e:
                log.warning("FloodWait %ss on alert send; sleeping", e.seconds)
                await asyncio.sleep(e.seconds)
                await client.send_message("me", msg)

    await client.start()
    me = await client.get_me()
    log.info("Watching %d supplier chats as %s", len(suppliers), me.first_name)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
