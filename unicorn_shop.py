"""Identify the Unicorn Magic Factory shop so SPBC back-room paths can skip it.

Unicorn checkout (vendor bot, Mini App POST /order, /webpanel, payment codes)
is local to that shop's rows. SPBC used to quote this shop, share its catalog,
and hand website orders to Ghostie's bot. Remy cut that back-room link only.
"""

from __future__ import annotations

import os
from typing import Any

import db

# Public read-only catalog key baked into remy-miniapp-demos.pages.dev/unicorn/.
# Not a claim/admin token. Override with UNICORN_STOREFRONT_KEY if Pages rotates.
PAGES_STOREFRONT_KEY = "dd6dec3482e1572886868657"

# Buyer-facing Unicorn rails (not secrets). Boot/admin seed inserts a type only
# when it is absent — paused rows block a re-seed. Pause instead of delete.
DEFAULT_PAYMENT_METHODS: tuple[dict[str, str], ...] = (
    {"method_type": "venmo", "handle": "@wineboos"},
    {
        "method_type": "paypal",
        "handle": "unicornfartzz@proton.me",
        "network_note": "friends_family",
    },
)

_PAID_STATUSES = ("paid", "shipped", "complete")

# Title fragments used when binding the live Ghostie shop (run_cloud / webpanel).
UNICORN_TITLE_MARKERS = (
    "unicorn",
    "magic factory",
    "unicorn magic",
    "unicorn fancy",
    "unicornmagicfactory",
    "@unicornmagicfactory",
)

# Default BRAND_NAME / group titles like "Ash, UnicornFartzzBot and Samantha"
# must not count as the customer shop (that bind served 0 products live).
_BRAND_FALSE_POSITIVES = (
    "unicornfartzzbot",
    "unicornfartzz",
)


def shop_title_looks_unicorn(title: str | None) -> bool:
    t = (title or "").strip().lower()
    if not t:
        return False
    for false in _BRAND_FALSE_POSITIVES:
        t = t.replace(false, " ")
    t = " ".join(t.split())
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


def pages_storefront_key() -> str:
    """24-hex catalog key the Cloudflare Pages Mini App sends as ?invite=."""
    raw = (os.getenv("UNICORN_STOREFRONT_KEY") or "").strip().strip("\"'")
    key = raw or PAGES_STOREFRONT_KEY
    if key.lower().startswith("vendor_"):
        key = key[7:]
    elif key.lower().startswith("vendor"):
        key = key[6:]
    return key.strip().lower()


def is_pages_storefront_key(raw_key: str | None) -> bool:
    got = (raw_key or "").strip()
    if got.lower().startswith("vendor_"):
        got = got[7:]
    elif got.lower().startswith("vendor"):
        got = got[6:]
    return got.strip().lower() == pages_storefront_key()


def is_unicorn_customer_bot() -> bool:
    """True when this process is @UnicornMagicFactory2Bot / Unicorn vendor token."""
    public = (os.getenv("PUBLIC_BOT_USERNAME") or "").strip().lstrip("@").lower()
    if "unicornmagicfactory" in public:
        return True
    if (os.getenv("UNICORN_BOT_TOKEN") or "").strip():
        return True
    return False


def _list_shops() -> list[dict]:
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM shops").fetchall()]


def _product_count(chat_id: int) -> int:
    """Active catalog rows only — empty/inactive shops must not win the bind."""
    try:
        return len(db.list_products(int(chat_id), active_only=True))
    except Exception:
        return 0


def _newest_paid_ts(chat_id: int) -> str:
    try:
        with db.get_db() as conn:
            placeholders = ",".join("?" for _ in _PAID_STATUSES)
            row = conn.execute(
                f"""
                SELECT MAX(COALESCE(paid_at, shipped_at, updated_at, created_at)) AS ts
                FROM orders
                WHERE chat_id = ?
                  AND status IN ({placeholders})
                """,
                (int(chat_id), *_PAID_STATUSES),
            ).fetchone()
    except Exception:
        return ""
    return str((row["ts"] if row and row["ts"] else "") or "")


def _pick_stocked(cands: list[dict]) -> dict | None:
    """Newest paid/shipped/complete, then most active products. None if all empty."""
    stocked = [s for s in cands if _product_count(int(s["chat_id"])) > 0]
    if not stocked:
        return None

    def _score(s: dict) -> tuple:
        sid = int(s["chat_id"])
        ts = _newest_paid_ts(sid)
        n = _product_count(sid)
        return (1 if ts else 0, ts, n)

    return sorted(stocked, key=_score, reverse=True)[0]


def find_catalog_shop() -> dict | None:
    """Shop whose catalog the Pages Mini App should show. Does not delete shops.

    Never binds a zero-product shop when any shop has active stock.
    Order: stocked UNICORN_SHOP_CHAT_ID pin → stocked Unicorn-titled shop
    (newest paid wins) → any stocked shop (newest paid). Empty env pins
    and brand-false-positive groups are skipped.
    """
    shops = _list_shops()
    if not shops:
        return None

    env_id = env_unicorn_shop_chat_id()
    if env_id is not None:
        try:
            sid = int(db.resolve_shop_chat_id(env_id))
        except Exception:
            sid = env_id
        shop = db.get_shop(sid) or db.get_shop(env_id)
        if shop and _product_count(int(shop["chat_id"])) > 0:
            return shop

    unicorns = [s for s in shops if shop_title_looks_unicorn(s.get("title"))]
    picked = _pick_stocked(unicorns)
    if picked:
        return picked
    return _pick_stocked(shops)
