"""One-time setup helper: list your dialogs so you can pick supplier chats.

Run:  python -m supplier_watch.list_chats
Then copy the chat_ids you want into supplier_watch/suppliers.json:
    {"chats": [{"chat_id": -1001234567890, "name": "Supplier X"}]}
"""

from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from . import config


async def main() -> None:
    if not config.TG_API_ID or not config.TG_API_HASH:
        sys.exit("Set TG_API_ID and TG_API_HASH in supplier_watch/.env first.")
    client = TelegramClient(str(config.SESSION_PATH), config.TG_API_ID, config.TG_API_HASH)
    async with client:
        print(f"{'chat_id':>15} | {'type':<8} | title")
        print("-" * 60)
        async for d in client.iter_dialogs():
            kind = ("channel" if d.is_channel else
                    "group" if d.is_group else "user")
            print(f"{d.id:>15} | {kind:<8} | {d.title}")


if __name__ == "__main__":
    asyncio.run(main())
