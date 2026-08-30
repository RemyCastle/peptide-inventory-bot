"""Identify the Unicorn Magic Factory shop so SPBC back-room paths can skip it.

Unicorn checkout (vendor bot, Mini App POST /order, /webpanel, payment codes)
is local to that shop's rows. SPBC used to quote this shop, share its catalog,
and hand website orders to Ghostie's bot. Remy cut that back-room link only.
"""

from __future__ import annotations

import os
from typing import Any

import db

# Title fragments used when binding the live Ghostie shop (run_cloud / webpanel).
UNICORN_TITLE_MARKERS = (
    "unicorn",
    "magic factory",
    "unicorn magic",
    "unicorn fancy",
    "unicornmagicfactory",
    "@unicornmagicfactory",
)


def shop_title_looks_unicorn(title: str | None) -> bool:
    t = (title or "").strip().lower()
    if not t:
        return False
    return any(m in t for m in UNICORN_TITLE_MARKERS)


def vendor_name_looks_unicorn(name: str | None) -> bool:
    return "unicorn" in (name or "").strip().lower()


def env_unicorn_shop_chat_id() -> int | None:
    raw = (os.getenv("UNICORN_SHOP_CHAT_ID") or "").strip().strip("\"'")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_unicorn_shop(chat_id: int | None, title: str | None = None) -> bool:
    """True when this shop is Unicorn Magic Factory (env id and/or title)."""
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        cid = 0
    if not cid:
        return shop_title_looks_unicorn(title)

    env_id = env_unicorn_shop_chat_id()
    if env_id is not None:
        try:
            if cid == int(db.resolve_shop_chat_id(env_id)) or cid == env_id:
                return True
        except Exception:
            if cid == env_id:
                return True

    if title is None:
        try:
            shop = db.get_shop(cid)
        except Exception:
            shop = None
        title = (shop or {}).get("title")
    return shop_title_looks_unicorn(title)


def is_unicorn_vendor(v: dict[str, Any] | None, shop_chat_id: int | None = None) -> bool:
    """Vendor config and/or resolved shop belong to Unicorn Magic Factory."""
    cfg = v or {}
    if vendor_name_looks_unicorn(cfg.get("name")):
        return True
    sid = shop_chat_id
    if sid in (None, ""):
        raw = cfg.get("shop_chat_id")
        try:
            sid = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            sid = None
    return is_unicorn_shop(sid)


def vendor_accepts_spbc_fulfillment(
    v: dict[str, Any] | None, shop_chat_id: int | None = None
) -> bool:
    """Other vendor bots may still accept SPBC offers; Unicorn must not."""
    return not is_unicorn_vendor(v, shop_chat_id)
