"""Identify the Unicorn Magic Factory shop so SPBC back-room paths can skip it.

Unicorn checkout (vendor bot, Mini App POST /order, /webpanel, payment codes)
is local to that shop's rows. SPBC used to quote this shop, share its catalog,
and hand website orders to Ghostie's bot. Remy cut that back-room link only.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

import db

log = logging.getLogger("unicorn_shop")

# Public read-only catalog key baked into remy-miniapp-demos.pages.dev/unicorn/.
# Not a claim/admin token. Override with UNICORN_STOREFRONT_KEY if Pages rotates.
PAGES_STOREFRONT_KEY = "dd6dec3482e1572886868657"

# Live Ghostie catalog on suspended spbc-supplier-bot (2026-09-10 bind logs).
CANONICAL_SHOP_CHAT_ID = 9_100_000_000_000
CANONICAL_SHOP_TITLE = "@unicornmagicfactory"
DEFAULT_CATALOG_MIRROR = (
    "https://spbc-supplier-bot.onrender.com/storefront"
    f"?invite={PAGES_STOREFRONT_KEY}"
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

    Never binds a zero-product shop when a Unicorn shop has active stock.
    Order: stocked UNICORN_SHOP_CHAT_ID pin → stocked canonical
    9100000000000 → stocked Unicorn-titled shop (newest paid). Does not
    fall through to unrelated stocked shops (live #15 bound a 312-product
    generic \"Shop\" that is not Ghostie's catalog).
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

    canon = db.get_shop(CANONICAL_SHOP_CHAT_ID)
    if canon and _product_count(CANONICAL_SHOP_CHAT_ID) > 0:
        return canon

    unicorns = [s for s in shops if shop_title_looks_unicorn(s.get("title"))]
    return _pick_stocked(unicorns)


def skip_bot_polling() -> bool:
    """HTTP-only boot (wake supplier-bot catalog without a second Telegram poller)."""
    return (os.getenv("SKIP_BOT_POLLING") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def catalog_mirror_url() -> str:
    raw = (os.getenv("UNICORN_CATALOG_MIRROR_URL") or "").strip().strip("\"'")
    return raw or DEFAULT_CATALOG_MIRROR


def ensure_canonical_shop() -> dict:
    """Create shop 9100000000000 (@unicornmagicfactory) if missing. No deletes."""
    shop = db.get_shop(CANONICAL_SHOP_CHAT_ID)
    if shop:
        title = (shop.get("title") or "").strip()
        if not shop_title_looks_unicorn(title):
            db.update_shop(CANONICAL_SHOP_CHAT_ID, title=CANONICAL_SHOP_TITLE)
            shop = db.get_shop(CANONICAL_SHOP_CHAT_ID) or shop
        return shop
    return db.ensure_shop(CANONICAL_SHOP_CHAT_ID, title=CANONICAL_SHOP_TITLE)


def import_storefront_payload(dest_chat_id: int, payload: dict[str, Any]) -> int:
    """Copy public /storefront JSON into dest_chat_id. Add-only (no deletes)."""
    if not isinstance(payload, dict) or not payload.get("ok"):
        return 0
    dest = int(dest_chat_id)
    db.ensure_shop(dest, title=CANONICAL_SHOP_TITLE)
    meta = payload.get("shop") if isinstance(payload.get("shop"), dict) else {}
    updates: dict[str, Any] = {}
    title = str(meta.get("title") or CANONICAL_SHOP_TITLE).strip()[:80]
    if title:
        updates["title"] = title
    if meta.get("shipping_enabled") is not None:
        updates["shipping_enabled"] = int(meta.get("shipping_enabled") or 0)
    if meta.get("shipping_fee") is not None:
        updates["shipping_fee"] = float(meta.get("shipping_fee") or 0)
    if meta.get("free_shipping_above") is not None:
        updates["free_shipping_above"] = float(meta.get("free_shipping_above") or 0)
    zones = meta.get("shipping_zones")
    if zones is not None:
        updates["shipping_zones"] = (
            zones if isinstance(zones, str) else json.dumps(zones)
        )
    if updates:
        db.update_shop(dest, **updates)

    existing = {(p.get("name") or "").strip().lower() for p in db.list_products(dest)}
    created = 0
    for raw in payload.get("products") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or name.lower() in existing:
            continue
        pid = db.add_product(
            dest,
            name,
            float(raw.get("price") or 0),
            int(raw.get("stock") or 0),
        )
        extras: dict[str, Any] = {}
        kit = raw.get("kit_price")
        if kit not in (None, ""):
            try:
                extras["kit_price"] = float(kit)
            except (TypeError, ValueError):
                pass
        sku = raw.get("sku")
        if sku:
            extras["sku"] = str(sku).strip()[:40]
        vg = raw.get("variant_group")
        if vg:
            extras["variant_group"] = str(vg).strip()[:80]
        vl = raw.get("variant_label")
        if vl:
            extras["variant_label"] = str(vl).strip()[:80]
        cat = raw.get("category")
        if cat:
            extras["category"] = str(cat).strip()[:80]
        photo = (raw.get("photo_url") or "").strip()
        if photo.startswith("http"):
            extras["photo_file_id"] = photo
        if raw.get("sort_order") is not None:
            try:
                extras["sort_order"] = int(raw.get("sort_order") or 0)
            except (TypeError, ValueError):
                pass
        if extras:
            db.update_product(pid, **extras)
        existing.add(name.lower())
        created += 1
    return created


def fetch_storefront_json(url: str, timeout: float = 20.0) -> dict[str, Any] | None:
    target = (url or "").strip()
    if not target.startswith("https://") and not target.startswith("http://"):
        return None
    req = urllib.request.Request(
        target,
        headers={
            "Accept": "application/json",
            "User-Agent": "unicornfartzz-catalog-pull/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("catalog mirror fetch failed url=%s err=%s", target.split("?")[0], exc)
        return None
    return data if isinstance(data, dict) else None


def import_catalog_if_empty(dest_chat_id: int | None = None) -> int:
    """Pull supplier-bot /storefront into the canonical shop when this disk is empty."""
    sid = int(dest_chat_id or CANONICAL_SHOP_CHAT_ID)
    ensure_canonical_shop()
    if _product_count(sid) > 0:
        return 0
    payload = fetch_storefront_json(catalog_mirror_url())
    if not payload:
        return 0
    n = import_storefront_payload(sid, payload)
    log.info("catalog mirror imported products=%s shop=%s", n, sid)
    return n


def ensure_pages_catalog() -> dict | None:
    """Make sure the Pages invite can resolve a shop; import stock if this disk is empty."""
    ensure_canonical_shop()
    try:
        import_catalog_if_empty(CANONICAL_SHOP_CHAT_ID)
    except Exception:
        log.exception("catalog mirror import failed")
    shop = find_catalog_shop()
    if shop:
        return shop
    return db.get_shop(CANONICAL_SHOP_CHAT_ID)
