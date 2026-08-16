"""On-demand digest: cheapest current source per product → Saved Messages.

Run:  python -m supplier_watch.digest          (print only)
      python -m supplier_watch.digest --send   (also send to Saved Messages)
"""

from __future__ import annotations

import asyncio
import sys
from itertools import groupby

from . import config, db


def build_digest() -> str:
    conn = db.connect(config.DB_PATH)
    rows = db.cheapest_per_product(conn)
    if not rows:
        return "No supplier prices recorded yet."
    lines = ["🏷️ Cheapest source per product"]
    for key, grp in groupby(rows, key=lambda r: r["product_key"]):
        best = next(grp)  # rows are price-ascending within product
        size = f" {best['size']}" if best["size"] else ""
        cur = "$" if best["currency"] == "USD" else f"{best['currency']} "
        lines.append(f"• {best['product']}{size}: {cur}{best['price']:g} ({best['supplier']})")
    return "\n".join(lines)


async def send(text: str) -> None:
    from telethon import TelegramClient
    client = TelegramClient(str(config.SESSION_PATH), config.TG_API_ID, config.TG_API_HASH)
    async with client:
        await client.send_message("me", text)


if __name__ == "__main__":
    digest = build_digest()
    print(digest)
    if "--send" in sys.argv:
        asyncio.run(send(digest))
