"""Website catalog → Telegram shop sync.

Two ways a shop gets a website feed:

1. Instance-level SPBC sync (env: SPBC_SITE_URL + SPBC_SHOP_CHAT_ID) — the
   springfieldpbc.com storefront mirrored into one shop. Owner /syncsite.
2. Per-shop site links (`shop_site_links` table) — any shop admin connects
   their own site via /linksite <url> or Admin Panel → Site Links. Multiple
   links per shop are fine; each link's products are namespaced.

Feed contract: GET <url>/api/products (or the exact URL if it already returns
JSON) → {"products": [...]}. Item shapes accepted:

- SPBC shape:    {id, name, vial_price, pack_price, kit_only, sort_order, active}
- generic shape: {id|sku, name, price, kit_price?, stock?, unit?, description?,
                  active?, sort_order?}

Rules (see SITE-LINKING.txt for the admin-facing version):
- Site owns name, prices, active flag, sort order.
- Stock: only followed when the feed provides it (generic shape); otherwise
  bot-managed, and new products arrive at stock 0 (unbuyable until set).
- Products that vanish from the feed are deactivated, never deleted.
- Manually added bot products (no site_key) are never touched.

Products are matched by `site_key` = "<namespace><feed id>", with a one-time
case-insensitive name-match adoption for pre-existing rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import db
from config import (
    KIT_SIZE,
    SITE_SYNC_INTERVAL_MIN,
    SPBC_SHOP_CHAT_ID,
    SPBC_SITE_URL,
)

log = logging.getLogger("site_sync")

FETCH_TIMEOUT_SEC = 20
NEW_PRODUCT_STOCK = 0
USER_AGENT = "SPBC-InventoryBot/1.0 (+render)"
# Namespace prefixes keep the env-level SPBC sync and per-shop links disjoint
ENV_PREFIX = "S:"
MAX_LINKS_PER_SHOP = 5


class SiteSyncError(Exception):
    pass


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deactivated)

    def summary(self) -> str:
        lines = ["🔄 Site catalog sync"]
        if self.created:
            lines.append(f"➕ Created ({len(self.created)}):")
            lines += [f"  • {n}" for n in self.created[:15]]
            if len(self.created) > 15:
                lines.append(f"  … and {len(self.created) - 15} more")
        if self.updated:
            lines.append(f"✏️ Updated ({len(self.updated)}):")
            lines += [f"  • {n}" for n in self.updated[:15]]
            if len(self.updated) > 15:
                lines.append(f"  … and {len(self.updated) - 15} more")
        if self.deactivated:
            lines.append(f"🚫 Deactivated (gone from site): {len(self.deactivated)}")
            lines += [f"  • {n}" for n in self.deactivated[:15]]
        lines.append(f"✅ Unchanged: {self.unchanged}")
        return "\n".join(lines)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Schema ───────────────────────────────────────────────────────────────────

def _ensure_site_key_column() -> None:
    with db.get_db() as conn:
        cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()
        }
        if "site_key" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN site_key TEXT")


def ensure_site_links_table() -> None:
    with db.get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shop_site_links (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                url          TEXT NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
                last_sync_at TEXT,
                last_status  TEXT,
                created_at   TEXT NOT NULL,
                UNIQUE (chat_id, url)
            )
            """
        )


# ── Feed fetch + normalization ───────────────────────────────────────────────

def _feed_url(base_or_feed: str) -> str:
    u = (base_or_feed or "").strip().rstrip("/")
    if not u:
        raise SiteSyncError("No URL")
    # Accept either a site base URL or a direct feed URL
    return u if "/api/" in u or u.endswith(".json") else f"{u}/api/products"


def _fetch_products_raw(url: str) -> list[dict]:
    req = urllib.request.Request(
        _feed_url(url),
        headers={
            "X-SPBC-Member": "1",
            "Accept": "application/json",
            # Cloudflare 403s the default Python-urllib user agent
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SiteSyncError(f"Site API HTTP {exc.code}") from exc
    except Exception as exc:
        raise SiteSyncError(f"Site API unreachable: {exc}") from exc
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list):
        raise SiteSyncError('Site API returned no "products" list')
    return [p for p in products if isinstance(p, dict) and p.get("name")]


def fetch_site_products(base_url: str | None = None) -> list[dict]:
    """SPBC storefront feed (env-level sync)."""
    base = (base_url or SPBC_SITE_URL or "").strip()
    if not base:
        raise SiteSyncError("SPBC_SITE_URL not set")
    return [p for p in _fetch_products_raw(base) if p.get("id") is not None]


def _norm(name: str) -> str:
    return " ".join(str(name).split()).lower()


def _spbc_fields(p: dict) -> dict:
    """SPBC shape → bot product fields (stock stays bot-managed)."""
    name = " ".join(str(p["name"]).split())
    vial = p.get("vial_price")
    pack = p.get("pack_price")
    kit_only = bool(p.get("kit_only"))
    if kit_only or vial in (None, 0):
        # Sold only as a kit/pack: the kit IS the purchase unit
        price = float(pack or 0)
        kit_price = None
        unit = "kit"
    else:
        price = float(vial)
        # pack of KIT_SIZE vials maps onto the bot's kit pricing
        kit_price = float(pack) if pack not in (None, 0) else None
        unit = "vial"
    return {
        "key": str(p.get("id") or name),
        "name": name,
        "price": price,
        "kit_price": kit_price,
        "unit": unit,
        "description": "",
        "active": 1 if p.get("active", True) else 0,
        "sort_order": int(p.get("sort_order") or 0),
        "stock": None,
    }


def _generic_fields(p: dict) -> dict | None:
    """Generic shape → bot product fields (stock followed when provided)."""
    name = " ".join(str(p["name"]).split())
    try:
        price = float(p["price"])
    except (TypeError, ValueError, KeyError):
        return None
    kit_price = None
    if p.get("kit_price") not in (None, "", 0):
        try:
            kit_price = float(p["kit_price"])
        except (TypeError, ValueError):
            kit_price = None
    stock = None
    if p.get("stock") is not None:
        try:
            stock = max(0, int(p["stock"]))
        except (TypeError, ValueError):
            stock = None
    return {
        "key": str(p.get("id") or p.get("sku") or name).strip(),
        "name": name,
        "price": price,
        "kit_price": kit_price,
        "unit": (str(p.get("unit") or "vial").strip() or "vial")[:20],
        "description": str(p.get("description") or "").strip()[:500],
        "active": 1 if p.get("active", True) else 0,
        "sort_order": int(p.get("sort_order") or 0),
        "stock": stock,
    }


def normalize_item(p: dict) -> dict | None:
    if not isinstance(p, dict) or not p.get("name"):
        return None
    if "price" in p:
        return _generic_fields(p)
    if "pack_price" in p or "vial_price" in p:
        return _spbc_fields(p)
    return None


# ── Sync core ────────────────────────────────────────────────────────────────

def _sync_items(shop_id: int, items: list[dict], prefix: str) -> SyncResult:
    """Upsert normalized feed items into a shop under a site_key namespace."""
    _ensure_site_key_column()
    db.ensure_shop(shop_id)

    existing = db.list_products(shop_id, active_only=False)
    by_site_key = {
        str(p["site_key"]): p
        for p in existing
        if p.get("site_key") and str(p["site_key"]).startswith(prefix)
    }
    by_name = {_norm(p["name"]): p for p in existing}

    result = SyncResult()
    seen_keys: set[str] = set()

    for want in items:
        key = prefix + want["key"]
        seen_keys.add(key)
        row = by_site_key.get(key) or by_name.get(_norm(want["name"]))

        if row is None:
            pid = db.add_product(
                shop_id,
                want["name"],
                want["price"],
                stock=want["stock"] if want["stock"] is not None else NEW_PRODUCT_STOCK,
                description=want["description"],
                unit=want["unit"],
            )
            db.update_product(
                pid,
                kit_price=want["kit_price"],
                active=want["active"],
                sort_order=want["sort_order"],
            )
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE products SET site_key = ? WHERE id = ?", (key, pid)
                )
            result.created.append(want["name"])
            continue

        # Never adopt a row already owned by a different feed
        row_key = str(row.get("site_key") or "")
        if row_key and row_key != key and not row_key.startswith(prefix):
            # belongs to another namespace — treat as missing, create fresh
            pid = db.add_product(
                shop_id,
                want["name"],
                want["price"],
                stock=want["stock"] if want["stock"] is not None else NEW_PRODUCT_STOCK,
                description=want["description"],
                unit=want["unit"],
            )
            db.update_product(
                pid,
                kit_price=want["kit_price"],
                active=want["active"],
                sort_order=want["sort_order"],
            )
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE products SET site_key = ? WHERE id = ?", (key, pid)
                )
            result.created.append(want["name"])
            continue

        changes: dict = {}
        for f in ("name", "price", "kit_price", "unit", "active", "sort_order"):
            current = row.get(f)
            if f in ("price", "kit_price"):
                cur_v = None if current is None else float(current)
                new_v = None if want[f] is None else float(want[f])
                if cur_v != new_v:
                    changes[f] = want[f]
            elif f == "active":
                if int(current or 0) != int(want[f]):
                    changes[f] = want[f]
            elif str(current or "") != str(want[f]):
                changes[f] = want[f]
        # Stock only when the feed provides it
        if want["stock"] is not None and int(row.get("stock") or 0) != want["stock"]:
            changes["stock"] = want["stock"]

        if changes:
            db.update_product(int(row["id"]), **changes)
        if str(row.get("site_key") or "") != key:
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE products SET site_key = ? WHERE id = ?",
                    (key, int(row["id"])),
                )
        if changes:
            result.updated.append(want["name"])
        else:
            result.unchanged += 1

    for key, row in by_site_key.items():
        if key not in seen_keys and int(row.get("active") or 0) == 1:
            db.update_product(int(row["id"]), active=0)
            result.deactivated.append(str(row["name"]))

    log.info(
        "site_sync shop=%s prefix=%s created=%s updated=%s deactivated=%s unchanged=%s",
        shop_id,
        prefix,
        len(result.created),
        len(result.updated),
        len(result.deactivated),
        result.unchanged,
    )
    return result


# ── Instance-level SPBC sync (env-configured) ───────────────────────────────

def sync_shop(chat_id: int | None = None, base_url: str | None = None) -> SyncResult:
    """Mirror the SPBC storefront into the configured shop. Thread-safe."""
    shop_id = int(chat_id if chat_id is not None else (SPBC_SHOP_CHAT_ID or 0))
    if not shop_id:
        raise SiteSyncError("SPBC_SHOP_CHAT_ID not set")
    items = [w for w in (normalize_item(p) for p in fetch_site_products(base_url)) if w]
    db.ensure_shop(shop_id, title="SPBC Shop")
    return _sync_items(shop_id, items, ENV_PREFIX)


# ── Per-shop site links ──────────────────────────────────────────────────────

def list_links(chat_id: int) -> list[dict]:
    ensure_site_links_table()
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM shop_site_links WHERE chat_id = ? AND active = 1 "
            "ORDER BY id",
            (int(chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_link(link_id: int) -> dict | None:
    ensure_site_links_table()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM shop_site_links WHERE id = ?", (int(link_id),)
        ).fetchone()
        return dict(row) if row else None


def add_link(chat_id: int, url: str) -> tuple[bool, str, int | None]:
    """Validate the feed, then store the link. Returns (ok, message, link_id)."""
    ensure_site_links_table()
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        return False, "URL must start with https://", None
    if len(list_links(chat_id)) >= MAX_LINKS_PER_SHOP:
        return False, f"Limit is {MAX_LINKS_PER_SHOP} linked sites per shop.", None
    try:
        raw = _fetch_products_raw(url)
    except SiteSyncError as exc:
        return False, f"Could not read the feed: {exc}", None
    items = [w for w in (normalize_item(p) for p in raw) if w]
    if not items:
        return (
            False,
            "Feed reachable but no usable products "
            "(need name + price, or name + vial_price/pack_price).",
            None,
        )
    with db.get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO shop_site_links (chat_id, url, active, created_at) "
                "VALUES (?, ?, 1, ?)",
                (int(chat_id), url, _utc_now()),
            )
            link_id = int(cur.lastrowid)
        except Exception:
            # UNIQUE(chat_id, url) — reactivate if it was removed before
            conn.execute(
                "UPDATE shop_site_links SET active = 1 WHERE chat_id = ? AND url = ?",
                (int(chat_id), url),
            )
            row = conn.execute(
                "SELECT id FROM shop_site_links WHERE chat_id = ? AND url = ?",
                (int(chat_id), url),
            ).fetchone()
            link_id = int(row["id"])
    return True, f"Linked. Found {len(items)} products in the feed.", link_id


def remove_link(link_id: int, chat_id: int) -> bool:
    """Deactivate a link AND its synced products (stock preserved)."""
    link = get_link(link_id)
    if not link or int(link["chat_id"]) != int(chat_id):
        return False
    prefix = f"L{int(link_id)}:"
    with db.get_db() as conn:
        conn.execute(
            "UPDATE shop_site_links SET active = 0 WHERE id = ?", (int(link_id),)
        )
        conn.execute(
            "UPDATE products SET active = 0, updated_at = ? "
            "WHERE chat_id = ? AND site_key LIKE ?",
            (_utc_now(), int(chat_id), prefix + "%"),
        )
    return True


def sync_link(link: dict) -> SyncResult:
    """Sync one shop_site_links row; records last_sync_at/last_status."""
    prefix = f"L{int(link['id'])}:"
    try:
        raw = _fetch_products_raw(str(link["url"]))
        items = [w for w in (normalize_item(p) for p in raw) if w]
        result = _sync_items(int(link["chat_id"]), items, prefix)
        status = (
            f"ok +{len(result.created)} ~{len(result.updated)} "
            f"-{len(result.deactivated)} ={result.unchanged}"
        )
    except SiteSyncError as exc:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE shop_site_links SET last_sync_at = ?, last_status = ? "
                "WHERE id = ?",
                (_utc_now(), f"error: {exc}", int(link["id"])),
            )
        raise
    with db.get_db() as conn:
        conn.execute(
            "UPDATE shop_site_links SET last_sync_at = ?, last_status = ? "
            "WHERE id = ?",
            (_utc_now(), status, int(link["id"])),
        )
    return result


def sync_all_links() -> dict[int, str]:
    """Sync every active link (all shops). Returns link_id -> status line."""
    ensure_site_links_table()
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM shop_site_links WHERE active = 1 ORDER BY id"
        ).fetchall()
        links = [dict(r) for r in rows]
    out: dict[int, str] = {}
    for link in links:
        try:
            result = sync_link(link)
            out[int(link["id"])] = "changed" if result.changed else "unchanged"
        except SiteSyncError as exc:
            out[int(link["id"])] = f"error: {exc}"
            log.warning("Link %s (%s) sync failed: %s", link["id"], link["url"], exc)
    return out


# ── Periodic task ────────────────────────────────────────────────────────────

async def periodic_site_sync(app) -> None:
    """Background task started from post_init.

    Runs shortly after startup, then every SITE_SYNC_INTERVAL_MIN: the
    env-configured SPBC sync (if set) plus every shop's site links. DMs the
    first global owner only when the SPBC sync changed something or failed.
    """
    from config import OWNER_IDS

    interval = max(15, int(SITE_SYNC_INTERVAL_MIN)) * 60
    owner_id = min(OWNER_IDS) if OWNER_IDS else None
    await asyncio.sleep(20)  # let polling settle before first sync
    while True:
        if SPBC_SITE_URL and SPBC_SHOP_CHAT_ID:
            try:
                result = await asyncio.to_thread(sync_shop)
                if result.changed and owner_id:
                    try:
                        await app.bot.send_message(owner_id, result.summary())
                    except Exception:
                        log.warning("Could not DM sync summary to owner %s", owner_id)
            except SiteSyncError as exc:
                log.warning("Periodic SPBC sync failed: %s", exc)
                if owner_id:
                    try:
                        await app.bot.send_message(
                            owner_id, f"⚠️ Site catalog sync failed: {exc}"
                        )
                    except Exception:
                        pass
            except Exception as exc:
                log.error("Periodic SPBC sync error: %s", exc, exc_info=exc)
        try:
            await asyncio.to_thread(sync_all_links)
        except Exception as exc:
            log.error("Periodic link sync error: %s", exc, exc_info=exc)
        await asyncio.sleep(interval)
