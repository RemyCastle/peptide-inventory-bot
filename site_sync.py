"""Website catalog → Telegram shop sync (springfieldpbc.com /api/products).

Pulls the SPBC storefront's live product list (same server-to-server call the
spbc-orders worker makes: `X-SPBC-Member: 1`) and mirrors it into one bot shop:

- Match by `site_key` (site product id, stored in a new products column),
  falling back to a case-insensitive name match to adopt existing rows.
- Site is the source of truth for: name, vial price, kit price (pack_price),
  active flag, sort order.
- The site has NO stock counts, so stock stays bot-managed: new products are
  created with stock 0 (admin sets real counts; customers can't buy at 0).
- Site-sourced rows that disappear from the feed are deactivated, never deleted.
  Manually added bot products (no site_key) are never touched.

Triggered by the owner-only /syncsite command and an optional periodic task
(SITE_SYNC_INTERVAL_MIN) started from post_init.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

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
            lines.append(f"➕ Created ({len(self.created)}), stock 0 — set stock:")
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


def _ensure_site_key_column() -> None:
    with db.get_db() as conn:
        cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()
        }
        if "site_key" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN site_key TEXT")


def fetch_site_products(base_url: str | None = None) -> list[dict]:
    """GET {site}/api/products with the server-to-server member header."""
    base = (base_url or SPBC_SITE_URL or "").strip().rstrip("/")
    if not base:
        raise SiteSyncError("SPBC_SITE_URL not set")
    req = urllib.request.Request(
        f"{base}/api/products",
        headers={
            "X-SPBC-Member": "1",
            "Accept": "application/json",
            # Cloudflare 403s the default Python-urllib user agent
            "User-Agent": "SPBC-InventoryBot/1.0 (+render)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SiteSyncError(f"Site API HTTP {exc.code}") from exc
    except Exception as exc:
        raise SiteSyncError(f"Site API unreachable: {exc}") from exc
    products = data.get("products")
    if not isinstance(products, list):
        raise SiteSyncError("Site API returned no products list")
    out = []
    for p in products:
        if not isinstance(p, dict) or p.get("id") is None or not p.get("name"):
            continue
        out.append(p)
    return out


def _norm(name: str) -> str:
    return " ".join(str(name).split()).lower()


def _desired_fields(p: dict) -> dict:
    """Map one site product to bot product fields (excluding stock)."""
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
        "name": name,
        "price": price,
        "kit_price": kit_price,
        "unit": unit,
        "active": 1 if p.get("active", True) else 0,
        "sort_order": int(p.get("sort_order") or 0),
    }


def sync_shop(
    chat_id: int | None = None, base_url: str | None = None
) -> SyncResult:
    """Mirror the site catalog into the configured bot shop. Thread-safe."""
    shop_id = int(chat_id if chat_id is not None else (SPBC_SHOP_CHAT_ID or 0))
    if not shop_id:
        raise SiteSyncError("SPBC_SHOP_CHAT_ID not set")

    site_products = fetch_site_products(base_url)
    _ensure_site_key_column()
    db.ensure_shop(shop_id, title="SPBC Shop")

    existing = db.list_products(shop_id, active_only=False)
    by_site_key = {
        str(p["site_key"]): p for p in existing if p.get("site_key")
    }
    by_name = {_norm(p["name"]): p for p in existing}

    result = SyncResult()
    seen_keys: set[str] = set()

    for sp in site_products:
        key = str(sp["id"])
        seen_keys.add(key)
        want = _desired_fields(sp)
        row = by_site_key.get(key) or by_name.get(_norm(want["name"]))

        if row is None:
            pid = db.add_product(
                shop_id,
                want["name"],
                want["price"],
                stock=NEW_PRODUCT_STOCK,
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

        changes = {}
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

        needs_key = str(row.get("site_key") or "") != key
        if changes:
            db.update_product(int(row["id"]), **changes)
        if needs_key or changes:
            with db.get_db() as conn:
                if needs_key:
                    conn.execute(
                        "UPDATE products SET site_key = ? WHERE id = ?",
                        (key, int(row["id"])),
                    )
        if changes:
            result.updated.append(want["name"])
        else:
            result.unchanged += 1

    # Site-sourced products no longer on the site → deactivate (stock kept)
    for key, row in by_site_key.items():
        if key not in seen_keys and int(row.get("active") or 0) == 1:
            db.update_product(int(row["id"]), active=0)
            result.deactivated.append(str(row["name"]))

    log.info(
        "site_sync shop=%s created=%s updated=%s deactivated=%s unchanged=%s",
        shop_id,
        len(result.created),
        len(result.updated),
        len(result.deactivated),
        result.unchanged,
    )
    return result


async def periodic_site_sync(app) -> None:
    """Background task started from post_init when configured.

    Runs one sync shortly after startup, then every SITE_SYNC_INTERVAL_MIN.
    Messages the first global owner only when something changed or broke.
    """
    from config import OWNER_IDS

    interval = max(15, int(SITE_SYNC_INTERVAL_MIN)) * 60
    owner_id = min(OWNER_IDS) if OWNER_IDS else None
    await asyncio.sleep(20)  # let polling settle before first sync
    while True:
        try:
            result = await asyncio.to_thread(sync_shop)
            if result.changed and owner_id:
                try:
                    await app.bot.send_message(owner_id, result.summary())
                except Exception:
                    log.warning("Could not DM sync summary to owner %s", owner_id)
        except SiteSyncError as exc:
            log.warning("Periodic site sync failed: %s", exc)
            if owner_id:
                try:
                    await app.bot.send_message(
                        owner_id, f"⚠️ Site catalog sync failed: {exc}"
                    )
                except Exception:
                    pass
        except Exception as exc:
            log.error("Periodic site sync error: %s", exc, exc_info=exc)
        await asyncio.sleep(interval)
