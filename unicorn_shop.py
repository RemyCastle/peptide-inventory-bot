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

# Unique-name importer used this when real shelf counts were missing.
# Recall must never write this as a fallback.
PLACEHOLDER_STOCK = 10

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
    panel = (
        os.getenv("PANEL_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    ).strip().lower()
    if "unicornfartzz" in panel:
        return True
    return False


def shop_is_active(shop: dict | None) -> bool:
    """Missing/NULL active counts as visible (pre-column rows)."""
    if not shop:
        return False
    if "active" not in shop:
        return True
    val = shop.get("active")
    if val is None:
        return True
    try:
        return int(val) != 0
    except (TypeError, ValueError):
        return True


def _list_shops(*, active_only: bool = False) -> list[dict]:
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM shops").fetchall()]
    if not active_only:
        return rows
    return [s for s in rows if shop_is_active(s)]


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
    shops = _list_shops(active_only=True)
    if not shops:
        return None

    env_id = env_unicorn_shop_chat_id()
    if env_id is not None:
        try:
            sid = int(db.resolve_shop_chat_id(env_id))
        except Exception:
            sid = env_id
        shop = db.get_shop(sid) or db.get_shop(env_id)
        if (
            shop
            and shop_is_active(shop)
            and _product_count(int(shop["chat_id"])) > 0
        ):
            return shop

    unicorns = [s for s in shops if shop_title_looks_unicorn(s.get("title"))]
    picked = _pick_stocked(unicorns)
    if picked:
        return picked
    return _pick_stocked(shops)


def staff_shop_for_admin(user_id: int) -> dict | None:
    """Catalog shop when this user may admin it (owner or shop admin).

    MagicFactory2 /orders and the admin panel must list Mini App orders on
    the Pages catalog shop, not a personal /start shop or title-sorted extra.
    """
    shop = find_catalog_shop()
    if not shop:
        return None
    try:
        uid = int(user_id)
        sid = int(shop["chat_id"])
    except (TypeError, ValueError, KeyError):
        return None
    if not uid or not sid:
        return None
    try:
        if db.is_admin(sid, uid):
            return shop
    except Exception:
        return None
    return None


def recent_orders_snapshot(limit: int = 20) -> list[dict]:
    """Newest orders: id, payment_code, chat_id, status, username. Read-only."""
    try:
        n = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        n = 20
    try:
        with db.get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, payment_code, chat_id, status, username, created_at
                FROM orders
                ORDER BY id DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
    except Exception:
        return []
    out: list[dict] = []
    for r in rows:
        try:
            oid = int(r["id"])
            cid = int(r["chat_id"])
        except (TypeError, ValueError, KeyError):
            continue
        out.append(
            {
                "id": oid,
                "payment_code": str(r["payment_code"] or ""),
                "chat_id": cid,
                "status": str(r["status"] or ""),
                "username": str(r["username"] or ""),
                "created_at": str(r["created_at"] or ""),
            }
        )
    return out


def keep_only_catalog_shop() -> dict:
    """Reattach extra-shop orders to the catalog shop and soft-hide extras.

    Mini App POST /order writes to whichever shop the Pages key was bound to.
    Extra personal /start shops and old Unicorn-titled binds must not orphan
    those rows. Soft-sets shops.active=0 on every non-keeper shop. Never
    DELETE shops, products, or inventory.db. Idempotent.
    """
    db.init_db()
    shop = find_catalog_shop()
    if not shop:
        return {
            "ok": False,
            "skipped": "no catalog shop",
            "moved": 0,
            "stray_shops": 0,
            "deactivated": 0,
        }
    try:
        keeper = int(shop["chat_id"])
    except (TypeError, ValueError, KeyError):
        return {
            "ok": False,
            "skipped": "bad catalog shop",
            "moved": 0,
            "stray_shops": 0,
            "deactivated": 0,
        }

    moved = 0
    stray_shops = 0
    deactivated = 0
    try:
        with db.get_db() as conn:
            extras = conn.execute(
                "SELECT * FROM shops WHERE chat_id != ?",
                (keeper,),
            ).fetchall()
            for row in extras:
                extra = int(row["chat_id"])
                n_ord_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM orders WHERE chat_id = ?",
                    (extra,),
                ).fetchone()
                n_ord = int((n_ord_row["c"] if n_ord_row else 0) or 0)
                if n_ord > 0:
                    cur = conn.execute(
                        "UPDATE orders SET chat_id = ? WHERE chat_id = ?",
                        (keeper, extra),
                    )
                    n = int(cur.rowcount or 0)
                    if n:
                        moved += n
                        stray_shops += 1
                if shop_is_active(dict(row)):
                    conn.execute(
                        "UPDATE shops SET active = 0 WHERE chat_id = ?",
                        (extra,),
                    )
                    deactivated += 1
    except Exception:
        import logging

        logging.getLogger("unicorn_shop").exception("keep_only catalog failed")
        return {
            "ok": False,
            "skipped": "reattach failed",
            "moved": moved,
            "stray_shops": stray_shops,
            "deactivated": deactivated,
            "keeper": keeper,
        }

    try:
        import webpanel

        webpanel.ensure_storefront_key_plain(keeper, pages_storefront_key())
    except Exception:
        pass
    return {
        "ok": True,
        "keeper": keeper,
        "moved": moved,
        "stray_shops": stray_shops,
        "deactivated": deactivated,
    }


def _norm_product_name(name: str | None) -> str:
    try:
        from catalog_cleanup import sanitize_catalog_text

        s = sanitize_catalog_text(str(name or ""))
    except Exception:
        s = str(name or "")
    return " ".join(s.split()).casefold()


def _stocks_all_placeholder(stocks: list[int]) -> bool:
    if not stocks:
        return True
    return all(int(s) == PLACEHOLDER_STOCK for s in stocks)


def _sku_snapshot(products: list[dict]) -> dict[str, int]:
    n = len(products)
    nonzero = sum(1 for p in products if int(p.get("stock") or 0) > 0)
    tens = sum(1 for p in products if int(p.get("stock") or 0) == PLACEHOLDER_STOCK)
    return {"sku": n, "nonzero": nonzero, "placeholder_10": tens}


def _stock_index_from_rows(
    rows: list[dict],
    *,
    prefer_unicorn: bool = True,
    prefer_chat_id: int | None = None,
) -> dict[str, dict[str, int]]:
    """sku/name → stock. Keeper chat_id, then Unicorn-titled shops, overwrite."""
    ranked: list[tuple[int, dict]] = []
    prefer = int(prefer_chat_id) if prefer_chat_id else 0
    for p in rows:
        title = ""
        try:
            cid = int(p.get("chat_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        try:
            shop = db.get_shop(cid)
            title = (shop or {}).get("title") or ""
        except Exception:
            title = ""
        rank = 0
        if prefer_unicorn and shop_title_looks_unicorn(title):
            rank = 1
        if prefer and cid == prefer:
            rank = 2
        ranked.append((rank, p))
    ranked.sort(key=lambda t: t[0])  # low first; keeper overwrites
    idx: dict[str, dict[str, int]] = {"sku": {}, "name": {}}
    for _flag, p in ranked:
        try:
            stock = int(p.get("stock") or 0)
        except (TypeError, ValueError):
            continue
        if stock < 0:
            continue
        sku = str(p.get("sku") or "").strip().casefold()
        name = _norm_product_name(p.get("name"))
        if sku:
            _put_stock(idx["sku"], sku, stock)
        if name:
            _put_stock(idx["name"], name, stock)
    return idx


def _put_stock(bucket: dict[str, int], key: str, stock: int) -> None:
    """Keep a real shelf count; do not let placeholder 10s overwrite it."""
    if not key:
        return
    prev = bucket.get(key)
    if (
        prev is not None
        and prev != PLACEHOLDER_STOCK
        and stock == PLACEHOLDER_STOCK
    ):
        return
    bucket[key] = stock


def _index_has_stock(idx: dict[str, dict[str, int]]) -> bool:
    return bool(idx.get("sku") or idx.get("name"))


def _index_matches_live(idx: dict[str, dict[str, int]], live: list[dict]) -> bool:
    """True when at least one live catalog row shares a sku/name with idx."""
    sku_idx = idx.get("sku") or {}
    name_idx = idx.get("name") or {}
    if not sku_idx and not name_idx:
        return False
    for p in live:
        sku = str(p.get("sku") or "").strip().casefold()
        if sku and sku in sku_idx:
            return True
        name = _norm_product_name(p.get("name"))
        if name and name in name_idx:
            return True
    return False


def _vault_stock_map(
    keeper: int | None = None,
) -> tuple[dict[str, dict[str, int]], str]:
    """Load product stock from /data/backups or BACKUP_DIR. Empty if unusable."""
    import backup as backup_mod
    from pathlib import Path

    passphrase = backup_mod.passphrase_from_env()
    if not passphrase:
        return {"sku": {}, "name": {}}, "no_passphrase"

    dirs: list[Path] = []
    env_raw = (os.getenv("BACKUP_DIR") or "").strip()
    if env_raw:
        dirs.append(Path(env_raw))
    try:
        db_vault = Path(db.get_db_path()).resolve().parent / "backups"
        if db_vault not in dirs:
            dirs.append(db_vault)
    except Exception:
        pass
    data_vault = Path("/data/backups")
    if data_vault.is_dir() and data_vault not in dirs:
        dirs.append(data_vault)

    last_reason = "no_vault"
    for folder in dirs:
        enc = backup_mod.pick_vault_backup(folder)
        if enc is None:
            continue
        try:
            rows = backup_mod.read_products_from_encrypted_backup(enc, passphrase)
        except Exception:
            last_reason = "vault_decrypt_failed"
            continue
        stocks = []
        for r in rows:
            try:
                stocks.append(int(r.get("stock") or 0))
            except (TypeError, ValueError):
                continue
        if _stocks_all_placeholder(stocks):
            last_reason = "vault_placeholder"
            continue
        mapping = _stock_index_from_rows(
            rows, prefer_unicorn=True, prefer_chat_id=keeper
        )
        if _index_has_stock(mapping):
            return mapping, "vault"
        last_reason = "vault_empty"
    return {"sku": {}, "name": {}}, last_reason


def _other_unicorn_shop_stock_index(keeper: int) -> dict[str, dict[str, int]]:
    """Last known catalog stock sitting on extra (often inactive) shops.

    Unicorn-titled extras win. Other extras on this Unicorn process are a
    fallback for leftover /start binds titled Shop. All-placeholder sources
    are skipped.
    """
    unicorn_rows: list[dict] = []
    other_rows: list[dict] = []
    for shop in _list_shops(active_only=False):
        try:
            sid = int(shop["chat_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if sid == keeper:
            continue
        try:
            prods = db.list_products(sid, active_only=False)
        except Exception:
            continue
        if shop_title_looks_unicorn(shop.get("title")):
            unicorn_rows.extend(prods)
        else:
            other_rows.extend(prods)
    for rows in (unicorn_rows, other_rows):
        stocks = []
        for r in rows:
            try:
                stocks.append(int(r.get("stock") or 0))
            except (TypeError, ValueError):
                continue
        if _stocks_all_placeholder(stocks):
            continue
        return _stock_index_from_rows(
            rows, prefer_unicorn=True, prefer_chat_id=None
        )
    return {"sku": {}, "name": {}}


def _audit_stock_index(live: list[dict]) -> dict[str, dict[str, int]]:
    """Most recent non-placeholder stock_after/before per live product."""
    idx: dict[str, dict[str, int]] = {"sku": {}, "name": {}}
    for p in live:
        try:
            pid = int(p["id"])
        except (TypeError, ValueError, KeyError):
            continue
        try:
            audit = db.list_stock_audit(product_id=pid, limit=50)
        except Exception:
            audit = []
        chosen: int | None = None
        for row in audit:
            for key in ("stock_after", "stock_before"):
                raw = row.get(key)
                if raw is None:
                    continue
                try:
                    val = int(raw)
                except (TypeError, ValueError):
                    continue
                if val == PLACEHOLDER_STOCK:
                    continue
                if val < 0:
                    continue
                chosen = val
                break
            if chosen is not None:
                break
        if chosen is None:
            continue
        sku = str(p.get("sku") or "").strip().casefold()
        name = _norm_product_name(p.get("name"))
        if sku:
            idx["sku"][sku] = chosen
        if name:
            idx["name"][name] = chosen
    return idx


def _apply_stock_index(
    live: list[dict],
    idx: dict[str, dict[str, int]],
    *,
    source: str = "",
) -> tuple[int, int]:
    """Write recalled stock via adjust_stock. Returns (updated, skipped_placeholder)."""
    name_counts: dict[str, int] = {}
    for p in live:
        name = _norm_product_name(p.get("name"))
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1

    updated = 0
    skipped_placeholder = 0
    sku_idx = idx.get("sku") or {}
    name_idx = idx.get("name") or {}
    for p in live:
        try:
            old = int(p.get("stock") or 0)
            pid = int(p["id"])
        except (TypeError, ValueError, KeyError):
            continue
        sku = str(p.get("sku") or "").strip().casefold()
        name = _norm_product_name(p.get("name"))
        new: int | None = None
        if sku and sku in sku_idx:
            new = int(sku_idx[sku])
        if (
            new is None or new == PLACEHOLDER_STOCK
        ) and name and name_counts.get(name, 0) == 1 and name in name_idx:
            named = int(name_idx[name])
            if new is None or named != PLACEHOLDER_STOCK:
                new = named
        if new is None:
            continue
        if new < 0:
            continue
        if new == old:
            continue
        if new == PLACEHOLDER_STOCK and old != PLACEHOLDER_STOCK:
            skipped_placeholder += 1
            continue
        # Leftover /start shops often have stock 0 copies. Do not treat that
        # as last-good inventory over a live positive shelf count.
        if source == "catalog" and new == 0 and old > 0:
            continue
        delta = new - old
        if delta == 0:
            continue
        got = db.adjust_stock(pid, delta, reason="stock_recall")
        if got is not None:
            updated += 1
    return updated, skipped_placeholder


def recall_catalog_stock() -> dict:
    """Restore catalog shelf counts from vault / extra Unicorn shop / stock_audit.

    Never replaces inventory.db. Never invents placeholder 10s. Shop-scoped to
    find_catalog_shop(). Idempotent when live stock already matches the source.
    """
    db.init_db()
    marker = os.path.join(
        os.path.dirname(str(db.get_db_path())),
        ".oneshot-catalog-stock-100",
    )
    if os.path.isfile(marker):
        shop = find_catalog_shop()
        live = db.list_products(int(shop["chat_id"]), active_only=False) if shop else []
        snap = _sku_snapshot(live)
        return {
            "ok": True,
            "skipped": "oneshot_stock_100",
            "source": "oneshot",
            "updated": 0,
            "sku_before": snap["sku"],
            "sku_after": snap["sku"],
            "nonzero_before": snap["nonzero"],
            "nonzero_after": snap["nonzero"],
            "placeholder_10_before": snap["placeholder_10"],
            "placeholder_10_after": snap["placeholder_10"],
            "skipped_placeholder": 0,
            "keeper": int(shop["chat_id"]) if shop else 0,
        }
    shop = find_catalog_shop()
    if not shop:
        return {
            "ok": False,
            "skipped": "no catalog shop",
            "source": "none",
            "updated": 0,
            "sku_before": 0,
            "sku_after": 0,
            "nonzero_before": 0,
            "nonzero_after": 0,
            "skipped_placeholder": 0,
        }
    try:
        keeper = int(shop["chat_id"])
    except (TypeError, ValueError, KeyError):
        return {
            "ok": False,
            "skipped": "bad catalog shop",
            "source": "none",
            "updated": 0,
            "sku_before": 0,
            "sku_after": 0,
            "nonzero_before": 0,
            "nonzero_after": 0,
            "skipped_placeholder": 0,
        }

    live = db.list_products(keeper, active_only=False)
    before = _sku_snapshot(live)

    mapping, vault_reason = _vault_stock_map(keeper)
    source = "none"
    if _index_has_stock(mapping) and _index_matches_live(mapping, live):
        source = "vault"
    if source == "none":
        mapping = _other_unicorn_shop_stock_index(keeper)
        if _index_has_stock(mapping) and _index_matches_live(mapping, live):
            source = "catalog"
    if source == "none":
        mapping = _audit_stock_index(live)
        if _index_has_stock(mapping) and _index_matches_live(mapping, live):
            source = "stock_audit"

    if source == "none":
        after = _sku_snapshot(live)
        skipped = vault_reason if vault_reason not in ("vault",) else "no_source"
        return {
            "ok": True,
            "skipped": skipped,
            "source": "none",
            "updated": 0,
            "sku_before": before["sku"],
            "sku_after": after["sku"],
            "nonzero_before": before["nonzero"],
            "nonzero_after": after["nonzero"],
            "placeholder_10_before": before["placeholder_10"],
            "placeholder_10_after": after["placeholder_10"],
            "skipped_placeholder": 0,
            "keeper": keeper,
        }

    updated, skipped_placeholder = _apply_stock_index(
        live, mapping, source=source
    )
    after_live = db.list_products(keeper, active_only=False)
    after = _sku_snapshot(after_live)
    return {
        "ok": True,
        "source": source,
        "updated": updated,
        "sku_before": before["sku"],
        "sku_after": after["sku"],
        "nonzero_before": before["nonzero"],
        "nonzero_after": after["nonzero"],
        "placeholder_10_before": before["placeholder_10"],
        "placeholder_10_after": after["placeholder_10"],
        "skipped_placeholder": skipped_placeholder,
        "keeper": keeper,
    }
