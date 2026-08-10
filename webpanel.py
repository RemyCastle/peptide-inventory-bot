"""Vendor web console — a phone-friendly web face for one bot shop.

Served by the bot's existing HTTP server (spbc_notify routes /panel* here).
Auth is a magic link: /webpanel in Telegram issues a per-shop token
(sha256-hashed at rest, expiring, revocable) baked into the URL. The page
edits the SAME rows Telegram sells from — no second database, no sync drift.

Endpoints (JSON API is pure-function first for testability):
  GET  /panel?t=…               the single-file HTML app
  GET  /panel/api/state?t=…     shop + products + payments + shipping
  POST /panel/api/product       upsert one product (stock change → audit row)
  POST /panel/api/bulk          paste-import "name | price | stock" lines
  POST /panel/api/payment       add / update / delete a payment method
  POST /panel/api/shipping      shipping settings
  POST /panel/api/shop          shop title / welcome text

Vendor onboarding: /invitevendor (owner) → one-time deep link → redeeming it
creates the vendor's personal shop + admin rights + their panel link.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import db
import inventory_import
import payment_templates
from config import MEDIA_DIR, PANEL_BASE_URL

log = logging.getLogger("webpanel")

TOKEN_TTL_HOURS = 72
INVITE_TTL_HOURS = 14 * 24
MAX_BULK_BYTES = 100_000

# ── Uploads (product photos + COA files) ────────────────────────────────────
MAX_UPLOAD_BYTES = 6 * 1024 * 1024
# Declared mime is never trusted — the magic bytes decide.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"%PDF-", "application/pdf", "pdf"),
)
MEDIA_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(jpg|png|pdf)$")
_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
}


# Per-shop upload budget so a leaked panel link can't fill the disk
UPLOADS_PER_DAY = 60
UPLOAD_BYTES_PER_DAY = 60 * 1024 * 1024
_upload_budget: dict[int, dict] = {}


def _check_budget(chat_id: int, size: int) -> tuple[bool, str]:
    day = _utc_now().strftime("%Y-%m-%d")
    b = _upload_budget.get(int(chat_id))
    if not b or b["day"] != day:
        b = {"day": day, "count": 0, "bytes": 0}
        _upload_budget[int(chat_id)] = b
    if b["count"] >= UPLOADS_PER_DAY or b["bytes"] + size > UPLOAD_BYTES_PER_DAY:
        return False, "Daily upload limit reached — try again tomorrow."
    b["count"] += 1
    b["bytes"] += size
    return True, ""


def media_dir() -> Path:
    p = Path(MEDIA_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def sniff_kind(blob: bytes) -> Optional[tuple[str, str]]:
    """(mime, extension) from magic bytes, or None if not an allowed type."""
    for magic, mime, ext in _MAGIC:
        if blob.startswith(magic):
            return mime, ext
    return None


def save_upload(data_url: str) -> tuple[bool, str]:
    """Validate a data: URL and store it. Returns (ok, filename_or_error)."""
    raw = (data_url or "").strip()
    if not raw.startswith("data:"):
        return False, "Expected a data: URL"
    head, _, b64 = raw.partition(",")
    if not b64 or "base64" not in head:
        return False, "Upload must be base64 encoded"
    # Cheap size gate before decoding (base64 is ~4/3 of the payload)
    if len(b64) > MAX_UPLOAD_BYTES * 4 // 3 + 1024:
        return False, "File too large (max 6 MB)"
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        return False, "Could not read that file"
    if not blob or len(blob) > MAX_UPLOAD_BYTES:
        return False, "File too large (max 6 MB)"
    sniffed = sniff_kind(blob)
    if sniffed is None:
        return False, "Only JPG, PNG or PDF files are accepted"
    _mime, ext = sniffed
    name = f"{secrets.token_hex(16)}.{ext}"
    try:
        (media_dir() / name).write_bytes(blob)
    except OSError as exc:
        log.error("upload write failed: %s", exc)
        return False, "Could not save the file"
    return True, name


def media_url(filename: str) -> str:
    return f"{PANEL_BASE_URL.rstrip('/')}/media/{filename}"


def read_media(filename: str) -> Optional[tuple[bytes, str]]:
    """(bytes, content_type) for a stored file, or None. Traversal-safe."""
    if not MEDIA_NAME_RE.match(filename or ""):
        return None
    base = media_dir().resolve()
    path = (base / filename).resolve()
    # resolve() + prefix check: a crafted name can never escape the dir
    if path.parent != base or not path.is_file():
        return None
    ext = filename.rsplit(".", 1)[-1]
    try:
        return path.read_bytes(), _CONTENT_TYPES[ext]
    except OSError:
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_webpanel_tables() -> None:
    with db.get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS web_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS vendor_invites (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                note       TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_by    INTEGER,
                used_at    TEXT
            );
            -- Public read-only catalog key (Pages store). NEVER a claim credential.
            -- Separate namespace from vendor_invites so a leaked storefront URL
            -- cannot redeem /start vendor… into shop admin.
            CREATE TABLE IF NOT EXISTS storefront_keys (
                key_hash     TEXT PRIMARY KEY,
                key_plain    TEXT NOT NULL UNIQUE,
                shop_chat_id INTEGER NOT NULL UNIQUE,
                created_at   TEXT NOT NULL
            );
            """
        )
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(vendor_invites)").fetchall()
        }
        if "shop_chat_id" not in cols:
            # set when the owner pre-built the shop; claiming adopts it
            conn.execute("ALTER TABLE vendor_invites ADD COLUMN shop_chat_id INTEGER")


# ── Panel tokens ─────────────────────────────────────────────────────────────

def issue_token(chat_id: int, user_id: int, ttl_hours: int = TOKEN_TTL_HOURS) -> str:
    ensure_webpanel_tables()
    # hex, not token_urlsafe: '_' and '-' break Telegram Markdown links
    raw = secrets.token_hex(16)
    now = _utc_now()
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO web_tokens (token_hash, chat_id, user_id, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                _hash(raw),
                int(chat_id),
                int(user_id),
                _ts(now),
                _ts(now + timedelta(hours=ttl_hours)),
            ),
        )
    return raw


def revoke_tokens(chat_id: int) -> int:
    ensure_webpanel_tables()
    with db.get_db() as conn:
        cur = conn.execute(
            "UPDATE web_tokens SET revoked = 1 WHERE chat_id = ? AND revoked = 0",
            (int(chat_id),),
        )
        return cur.rowcount


def resolve_token(raw: str) -> Optional[dict]:
    """Valid, unexpired, unrevoked token → {chat_id, user_id}; else None."""
    if not raw:
        return None
    ensure_webpanel_tables()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM web_tokens WHERE token_hash = ?", (_hash(raw),)
        ).fetchone()
    if not row or int(row["revoked"]):
        return None
    if _ts(_utc_now()) > str(row["expires_at"]):
        return None
    return {"chat_id": int(row["chat_id"]), "user_id": int(row["user_id"])}


def panel_url(base_url: str, raw_token: str, mode: str = "") -> str:
    """mode='restock' opens straight into the shipment-receiving view."""
    url = f"{base_url.rstrip('/')}/panel?t={urllib.parse.quote(raw_token)}"
    if mode:
        url += f"&mode={urllib.parse.quote(mode)}"
    return url


# ── Vendor invites ───────────────────────────────────────────────────────────

def normalize_invite_token(raw_invite: str) -> str:
    """Strip handoff/storefront prefixes → 24-char hex invite body."""
    raw = (raw_invite or "").strip()
    if raw.startswith("vendor_"):
        raw = raw[len("vendor_") :]
    elif raw.startswith("vendor"):
        raw = raw[len("vendor") :]
    return raw.strip()


def create_vendor_invite(
    created_by: int, note: str = "", shop_chat_id: int | None = None
) -> str:
    """Invite token. With shop_chat_id, claiming adopts that pre-built shop."""
    ensure_webpanel_tables()
    # Telegram start payloads allow [A-Za-z0-9_-] up to 64 chars, but '_'/'-'
    # break Markdown parsing when the link is rendered — keep it hex.
    raw = secrets.token_hex(12)
    now = _utc_now()
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO vendor_invites (token_hash, note, created_by, created_at, "
            "expires_at, shop_chat_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _hash(raw),
                note.strip()[:80],
                int(created_by),
                _ts(now),
                _ts(now + timedelta(hours=INVITE_TTL_HOURS)),
                int(shop_chat_id) if shop_chat_id else None,
            ),
        )
    return raw


def redeem_vendor_invite(
    raw: str, user_id: int
) -> tuple[bool, str, Optional[int]]:
    """One-time redemption. Returns (ok, message, pre_built_shop_chat_id).

    Looks up vendor_invites only. A public storefront_keys catalog key can
    never redeem to admin (different table / namespace).
    """
    ensure_webpanel_tables()
    raw = normalize_invite_token(raw)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM vendor_invites WHERE token_hash = ?", (_hash(raw),)
        ).fetchone()
        if not row:
            return False, "Invite not found.", None
        if row["used_by"] is not None:
            return False, "Invite already used.", None
        if _ts(_utc_now()) > str(row["expires_at"]):
            return False, "Invite expired — ask for a fresh link.", None
        conn.execute(
            "UPDATE vendor_invites SET used_by = ?, used_at = ? WHERE id = ?",
            (int(user_id), _ts(_utc_now()), int(row["id"])),
        )
        shop_id = row["shop_chat_id"]
    return True, str(row["note"] or ""), (int(shop_id) if shop_id else None)


def _shop_product_count(chat_id: int, *, active_only: bool = True) -> int:
    try:
        return len(db.list_products(int(chat_id), active_only=active_only))
    except Exception:
        return 0


def _ensure_storefront_key(shop_chat_id: int) -> str:
    """Idempotent public catalog key for a shop (raw hex, for Pages / logs).

    Distinct from vendor_invites claim tokens. key_plain is stored because the
    key is intentionally public and boot must re-print it for Pages wiring.
    """
    ensure_webpanel_tables()
    sid = int(shop_chat_id)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT key_plain FROM storefront_keys WHERE shop_chat_id = ?",
            (sid,),
        ).fetchone()
        if row:
            return str(row["key_plain"])
        raw = secrets.token_hex(12)
        conn.execute(
            "INSERT INTO storefront_keys (key_hash, key_plain, shop_chat_id, "
            "created_at) VALUES (?, ?, ?, ?)",
            (_hash(raw), raw, sid, _ts(_utc_now())),
        )
        return raw


def _find_shop_for_miniapp(
    shop_chat_id: int | None = None,
    title_hints: list[str] | None = None,
) -> Optional[dict]:
    """Pick the pre-built vendor shop that should back a mini-app catalog.

    Fail closed: never guess across vendors. Heuristic matches require a
    title-hint hit, except when the DB has exactly one virtual shop.
    """
    if shop_chat_id is not None:
        shop = db.get_shop(int(shop_chat_id))
        if shop:
            return shop
        # Explicit id provided but missing — do not fall through to guessing
        log.warning(
            "miniapp shop: explicit shop_chat_id=%s not found — refusing heuristic",
            shop_chat_id,
        )
        return None

    hints = [h.lower() for h in (title_hints or []) if h and h.strip()]
    shops = db.list_shops() if hasattr(db, "list_shops") else None
    if shops is None:
        with db.get_db() as conn:
            shops = [dict(r) for r in conn.execute("SELECT * FROM shops").fetchall()]

    if not shops:
        return None

    rows: list[dict] = []
    for s in shops:
        sid = int(s["chat_id"])
        title = (s.get("title") or "").lower()
        hint_hits = sum(1 for h in hints if h in title)
        n_active = _shop_product_count(sid, active_only=True)
        n_all = _shop_product_count(sid, active_only=False)
        n = n_active if n_active > 0 else n_all
        is_virtual = sid >= db.VIRTUAL_SHOP_BASE
        rows.append(
            {
                "shop": s,
                "sid": sid,
                "title": s.get("title"),
                "hint_hits": hint_hits,
                "n": n,
                "n_active": n_active,
                "is_virtual": is_virtual,
            }
        )
        log.info(
            "miniapp shop scan chat_id=%s title=%r products=%s active=%s hints=%s virtual=%s",
            sid,
            s.get("title"),
            n,
            n_active,
            hint_hits,
            int(is_virtual),
        )

    def _pick(cands: list[dict]) -> Optional[dict]:
        if not cands:
            return None
        cands = sorted(
            cands,
            key=lambda r: (r["hint_hits"], r["n"], int(r["is_virtual"])),
            reverse=True,
        )
        return cands[0]["shop"]

    # 1) Title match + stocked (true handoff inventory for this brand)
    picked = _pick([r for r in rows if r["hint_hits"] > 0 and r["n"] > 0])
    if picked:
        return picked
    # 2) Title match even if empty (bind is diagnosable; catalog may be empty)
    picked = _pick([r for r in rows if r["hint_hits"] > 0])
    if picked:
        return picked
    # 3) Sole virtual shop in the whole DB (safe only when there is no choice)
    virtuals = [r for r in rows if r["is_virtual"]]
    if len(virtuals) == 1:
        return virtuals[0]["shop"]
    # Never bind an arbitrary stocked/empty virtual shop among several vendors
    log.warning(
        "miniapp shop: no title match among %s shops (%s virtual) — refusing bind",
        len(rows),
        len(virtuals),
    )
    return None


def ensure_miniapp_storefront(
    raw_invite: str,
    *,
    shop_chat_id: int | None = None,
    title_hints: list[str] | None = None,
    created_by: int = 0,
    note: str = "",
) -> dict[str, Any]:
    """Idempotently bind a claim invite to a shop AND issue a storefront key.

    - vendor_invites: private claim credential (/start vendor… → admin).
    - storefront_keys: public catalog key returned as storefront_key for Pages.
    These MUST stay different secrets. Claim is not required for /storefront.
    """
    ensure_webpanel_tables()
    raw = normalize_invite_token(raw_invite)
    if not re.fullmatch(r"[0-9a-fA-F]{24}", raw):
        return {"ok": False, "error": "bad_invite_token", "invite": raw_invite}

    # Already bound? Keep only if it still looks like the intended vendor shop.
    hints_l = [h.lower() for h in (title_hints or []) if h and h.strip()]
    with db.get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM vendor_invites WHERE token_hash = ?", (_hash(raw),)
        ).fetchone()
    if existing and existing["shop_chat_id"] and not shop_chat_id:
        sid = int(existing["shop_chat_id"])
        shop = db.get_shop(sid)
        n = _shop_product_count(sid)
        title_l = ((shop or {}).get("title") or "").lower()
        title_ok = (not hints_l) or any(h in title_l for h in hints_l)
        is_virtual = sid >= db.VIRTUAL_SHOP_BASE
        # When title hints are given, require a title match even for virtual shops
        # (a wrong prior virtual bind must not stick forever).
        if shop and n > 0 and title_ok:
            sf_key = _ensure_storefront_key(sid)
            return {
                "ok": True,
                "action": "already_bound",
                "shop_chat_id": sid,
                "title": shop.get("title"),
                "products": n,
                "storefront_key": sf_key,
            }
        log.info(
            "miniapp rebind: prior shop %s title=%r products=%s title_ok=%s virtual=%s",
            sid,
            (shop or {}).get("title"),
            n,
            title_ok,
            is_virtual,
        )

    shop = _find_shop_for_miniapp(shop_chat_id, title_hints)
    if not shop:
        return {"ok": False, "error": "no_shop_found"}

    sid = int(shop["chat_id"])
    title = (note or shop.get("title") or "Vendor shop").strip()[:80]
    now = _utc_now()
    # Long-lived claim row mapping; claim still one-shot via used_by.
    expires = now + timedelta(days=3650)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM vendor_invites WHERE token_hash = ?", (_hash(raw),)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE vendor_invites SET shop_chat_id = ?, note = ?, "
                "expires_at = ? WHERE token_hash = ?",
                (sid, title, _ts(expires), _hash(raw)),
            )
            action = "updated"
        else:
            conn.execute(
                "INSERT INTO vendor_invites (token_hash, note, created_by, "
                "created_at, expires_at, shop_chat_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _hash(raw),
                    title,
                    int(created_by),
                    _ts(now),
                    _ts(expires),
                    sid,
                ),
            )
            action = "created"

    sf_key = _ensure_storefront_key(sid)
    n = _shop_product_count(sid)
    log.info(
        "miniapp storefront %s claim→shop %s (%s) products=%s storefront_key=%s",
        action,
        sid,
        title,
        n,
        sf_key,
    )
    return {
        "ok": True,
        "action": action,
        "shop_chat_id": sid,
        "title": title,
        "products": n,
        "storefront_key": sf_key,
    }


# ── JSON API (pure functions; HTTP layer is a thin wrapper) ─────────────────

def _err(code: int, msg: str) -> tuple[int, dict]:
    return code, {"ok": False, "error": msg}


def api_storefront(raw_key: str) -> tuple[int, dict]:
    """Public, read-only catalog for a vendor's mini-app storefront.

    Keyed ONLY by storefront_keys (public catalog secret). Claim tokens from
    vendor_invites are rejected so a leaked Pages URL cannot double as admin
    claim. Exposes names, prices, kit prices, stock, shipping terms and
    payment-method names only — no instructions, no admin data.
    """
    ensure_webpanel_tables()
    raw = normalize_invite_token(raw_key)
    if not re.fullmatch(r"[0-9a-fA-F]{24}", raw):
        return _err(404, "unknown storefront")
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT shop_chat_id FROM storefront_keys WHERE key_hash = ?",
            (_hash(raw),),
        ).fetchone()
    if not row or not row["shop_chat_id"]:
        return _err(404, "unknown storefront")
    chat_id = db.resolve_shop_chat_id(int(row["shop_chat_id"]))
    shop = db.get_shop(chat_id) or db.ensure_shop(chat_id)
    products = db.list_products(chat_id, active_only=True)
    payments = db.list_payment_methods(chat_id, active_only=True)
    return 200, {
        "ok": True,
        "shop": {
            "title": shop["title"],
            "shipping_enabled": int(shop.get("shipping_enabled") or 0),
            "shipping_fee": float(shop.get("shipping_fee") or 0),
            "free_shipping_above": float(shop.get("free_shipping_above") or 0),
        },
        "products": [
            {
                "id": int(p["id"]),
                "name": p["name"],
                "price": float(p["price"]),
                "kit_price": (float(p["kit_price"]) if p.get("kit_price") else None),
                "stock": int(p.get("stock") or 0),
                "photo_url": ((p.get("photo_file_id") or "").strip()
                              if (p.get("photo_file_id") or "").startswith("http") else ""),
            }
            for p in products
        ],
        "payments": [m["name"] for m in payments],
    }


def _audit_stock(
    chat_id: int, product: dict, before: int, after: int, actor_id: int
) -> None:
    if before == after:
        return
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO stock_audit (chat_id, product_id, product_name, delta, "
            "stock_before, stock_after, reason, actor_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'web_panel', ?, ?)",
            (
                int(chat_id),
                int(product["id"]),
                str(product["name"]),
                after - before,
                before,
                after,
                int(actor_id),
                _ts(_utc_now()),
            ),
        )


def _product_public(p: dict) -> dict:
    photo = (p.get("photo_file_id") or "").strip()
    return {
        "id": p["id"],
        "name": p["name"],
        "price": p["price"],
        "kit_price": p.get("kit_price"),
        "stock": p.get("stock", 0),
        "unit": p.get("unit") or "vial",
        "active": int(p.get("active") or 0),
        "site_key": p.get("site_key"),
        # only URL photos can be shown in a browser (Telegram file_ids can't)
        "photo_url": photo if photo.startswith("http") else "",
        "has_photo": bool(photo),
        "coa_url": (p.get("coa_url") or "").strip(),
        "has_coa_file": bool((p.get("coa_file_id") or "").strip()),
    }


def api_state(tok: dict) -> tuple[int, dict]:
    chat_id = tok["chat_id"]
    shop = db.get_shop(chat_id) or db.ensure_shop(chat_id)
    products = db.list_products(chat_id, active_only=False)
    payments = db.list_payment_methods(chat_id, active_only=False)
    return 200, {
        "ok": True,
        "shop": {
            "chat_id": chat_id,
            "title": shop["title"],
            "welcome_text": shop.get("welcome_text") or "",
            "currency_symbol": "$",
            "shipping_enabled": int(shop.get("shipping_enabled") or 0),
            "shipping_fee": float(shop.get("shipping_fee") or 0),
            "free_shipping_above": float(shop.get("free_shipping_above") or 0),
        },
        "products": [_product_public(p) for p in products],
        "payments": [
            {
                "id": m["id"],
                "name": m["name"],
                "instructions": m.get("instructions") or "",
                "active": int(m.get("active") or 0),
                "method_type": (m.get("method_type") or "custom"),
                "cashtag": m.get("cashtag") or "",
                "handle": m.get("handle") or "",
                "chain": m.get("chain") or "",
                "address": m.get("address") or "",
                "network_note": m.get("network_note") or "",
            }
            for m in payments
        ],
    }


def api_product(tok: dict, payload: dict) -> tuple[int, dict]:
    chat_id = tok["chat_id"]
    name = " ".join(str(payload.get("name") or "").split())[:120]
    pid = payload.get("id")

    if pid is not None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return _err(400, "Bad product id")
        current = db.get_product(pid)
        if not current or int(current["chat_id"]) != int(chat_id):
            return _err(404, "Product not found in your shop")

    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name
    if payload.get("price") is not None:
        try:
            price = float(payload["price"])
        except (TypeError, ValueError):
            return _err(400, "Bad price")
        if price <= 0 or price > 1_000_000:
            return _err(400, "Price must be positive")
        fields["price"] = price
    if "kit_price" in payload:
        kp = payload["kit_price"]
        if kp in (None, "", 0, "0"):
            fields["kit_price"] = None
        else:
            try:
                kp = float(kp)
            except (TypeError, ValueError):
                return _err(400, "Bad kit price")
            if kp <= 0 or kp > 1_000_000:
                return _err(400, "Kit price must be positive")
            fields["kit_price"] = kp
    stock = None
    if payload.get("stock") is not None:
        try:
            stock = int(payload["stock"])
        except (TypeError, ValueError):
            return _err(400, "Bad stock")
        if stock < 0 or stock > 1_000_000:
            return _err(400, "Stock must be 0 or more")
    if payload.get("unit") is not None:
        unit = str(payload["unit"]).strip()[:20] or "vial"
        fields["unit"] = unit
    if payload.get("active") is not None:
        fields["active"] = 1 if payload["active"] in (1, True, "1", "true") else 0

    if pid is None:
        if not name:
            return _err(400, "Name required")
        if "price" not in fields:
            return _err(400, "Price required")
        new_id = db.add_product(
            chat_id,
            name,
            fields["price"],
            stock=stock or 0,
            unit=fields.get("unit", "vial"),
        )
        extra = {
            k: v for k, v in fields.items() if k in ("kit_price", "active")
        }
        if extra:
            db.update_product(new_id, **extra)
        if stock:
            _audit_stock(
                chat_id,
                {"id": new_id, "name": name},
                0,
                stock,
                tok["user_id"],
            )
        return 200, {"ok": True, "id": new_id, "created": True}

    if stock is not None:
        before = int(current.get("stock") or 0)
        fields["stock"] = stock
        _audit_stock(chat_id, current, before, stock, tok["user_id"])
    if not fields:
        return _err(400, "Nothing to update")
    db.update_product(pid, **fields)
    return 200, {"ok": True, "id": pid, "created": False}


def api_media(tok: dict, payload: dict) -> tuple[int, dict]:
    """Upload a product photo or COA file and attach it to a product."""
    chat_id = tok["chat_id"]
    kind = str(payload.get("kind") or "").strip()
    if kind not in ("photo", "coa"):
        return _err(400, "Unknown upload kind")
    try:
        pid = int(payload.get("id"))
    except (TypeError, ValueError):
        return _err(400, "Bad product id")
    current = db.get_product(pid)
    if not current or int(current["chat_id"]) != int(chat_id):
        return _err(404, "Product not found in your shop")

    # Clearing an existing file/link
    if payload.get("clear"):
        if kind == "photo":
            db.update_product(pid, photo_file_id=None)
        else:
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE products SET coa_url = NULL, coa_file_id = NULL, "
                    "coa_file_type = NULL, coa_filename = NULL, updated_at = ? "
                    "WHERE id = ? AND chat_id = ?",
                    (_ts(_utc_now()), pid, int(chat_id)),
                )
        return 200, {"ok": True, "cleared": True}

    # COA can be a plain link instead of a file
    link = str(payload.get("url") or "").strip()
    if kind == "coa" and link:
        ok, msg = db.set_product_coa_url(pid, int(chat_id), link)
        if not ok:
            return _err(400, msg)
        return 200, {"ok": True, "coa_url": msg}

    if not PANEL_BASE_URL:
        return _err(503, "Uploads are not configured on this deploy")
    data_url = str(payload.get("data_url") or "")
    okb, whyb = _check_budget(chat_id, len(data_url))
    if not okb:
        return _err(429, whyb)
    ok, result = save_upload(data_url)
    if not ok:
        return _err(400, result)
    url = media_url(result)
    if kind == "photo":
        if not result.endswith((".jpg", ".png")):
            return _err(400, "Product photos must be JPG or PNG")
        db.update_product(pid, photo_file_id=url)
        return 200, {"ok": True, "photo_url": url}
    ok2, msg2 = db.set_product_coa_url(pid, int(chat_id), url)
    if not ok2:
        return _err(400, msg2)
    return 200, {"ok": True, "coa_url": url}


def api_restock(tok: dict, payload: dict) -> tuple[int, dict]:
    """Shipment arrived: ADD to stock (never replace). {items:[{id, add}]}"""
    chat_id = tok["chat_id"]
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return _err(400, "Nothing to add")
    owned = {int(p["id"]): p for p in db.list_products(chat_id, active_only=False)}
    applied, skipped = [], []
    for it in items[:200]:
        if not isinstance(it, dict):
            continue
        try:
            pid = int(it.get("id"))
            add = int(it.get("add") or 0)
        except (TypeError, ValueError):
            continue
        if add == 0:
            continue
        if add < 0 or add > 100_000:
            skipped.append(f"#{pid}: bad quantity")
            continue
        row = owned.get(pid)
        if row is None:
            skipped.append(f"#{pid}: not in your shop")
            continue
        before = int(row.get("stock") or 0)
        after = before + add
        db.update_product(pid, stock=after)
        _audit_stock(chat_id, row, before, after, tok["user_id"])
        applied.append({"id": pid, "name": row["name"], "added": add, "stock": after})
    return 200, {
        "ok": True,
        "applied": applied,
        "count": len(applied),
        "skipped": skipped[:10],
    }


def api_bulk(tok: dict, payload: dict) -> tuple[int, dict]:
    text = str(payload.get("text") or "")
    if not text.strip():
        return _err(400, "Paste product lines first")
    if len(text.encode("utf-8", "ignore")) > MAX_BULK_BYTES:
        return _err(400, "Too much text at once")
    parsed, imported = inventory_import.import_from_text(
        tok["chat_id"], text, mode="upsert"
    )
    return 200, {
        "ok": True,
        "created": imported.created_count,
        "updated": imported.updated_count,
        "errors": (parsed.errors + imported.errors)[:20],
    }


def _payment_payload_to_fields(payload: dict) -> dict[str, Any]:
    """Normalize panel/API payment payload into DB columns + buyer instructions."""
    method_type = str(payload.get("method_type") or "custom").strip().lower()
    if method_type not in payment_templates.METHOD_TYPES:
        method_type = "custom"
    handle = str(payload.get("handle") or "").strip()
    address = str(payload.get("address") or "").strip()
    chain = str(payload.get("chain") or "").strip()
    network_note = str(payload.get("network_note") or "").strip()
    cashtag = str(payload.get("cashtag") or "").strip()
    name = str(payload.get("name") or "").strip()[:60]
    instructions = str(payload.get("instructions") or "").strip()[:1000]

    # Prefer structured type fields when present
    if method_type != "custom" or handle or address or cashtag:
        rendered = payment_templates.render_from_fields(
            method_type,
            handle=handle,
            address=address,
            chain=chain,
            network_note=network_note,
            cashtag=cashtag,
            name=name,
            instructions=instructions,
        )
        # Allow explicit name/instructions override after template render
        if name:
            rendered["name"] = name
        if payload.get("instructions") is not None and instructions:
            rendered["instructions"] = instructions
        return rendered

    return {
        "name": name or "Payment",
        "instructions": instructions,
        "method_type": "custom",
        "cashtag": None,
        "handle": None,
        "chain": None,
        "address": None,
        "network_note": None,
    }


def api_payment(tok: dict, payload: dict) -> tuple[int, dict]:
    chat_id = tok["chat_id"]
    mid = payload.get("id")
    if mid is not None:
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            return _err(400, "Bad payment id")
        current = db.get_payment_method(mid)
        if not current or int(current["chat_id"]) != int(chat_id):
            return _err(404, "Payment method not found in your shop")

    if payload.get("delete"):
        if mid is None:
            return _err(400, "Payment id required")
        db.delete_payment_method(mid)
        return 200, {"ok": True, "deleted": True}

    rendered = _payment_payload_to_fields(payload)
    name = str(rendered.get("name") or "").strip()[:60]
    instructions = str(rendered.get("instructions") or "").strip()[:1000]

    if mid is None:
        if not name:
            return _err(400, "Name required")
        # Require a usable handle/address for structured types
        mt = (rendered.get("method_type") or "custom").lower()
        if mt == "crypto" and not (rendered.get("address") or "").strip():
            return _err(400, "Crypto wallet address required")
        if mt in ("venmo", "paypal", "zelle", "apple_cash", "cashapp") and not (
            (rendered.get("handle") or rendered.get("cashtag") or "").strip()
        ):
            return _err(400, "Payment handle / number required")
        new_id = db.add_payment_from_template(chat_id, rendered)
        return 200, {"ok": True, "id": new_id, "created": True}

    fields: dict[str, Any] = {
        "name": name or "Payment",
        "instructions": instructions,
        "method_type": rendered.get("method_type"),
        "cashtag": rendered.get("cashtag"),
        "handle": rendered.get("handle"),
        "chain": rendered.get("chain"),
        "address": rendered.get("address"),
        "network_note": rendered.get("network_note"),
    }
    if payload.get("active") is not None:
        fields["active"] = 1 if payload["active"] in (1, True, "1", "true") else 0
    # Allow partial save of just active/name without wiping structured fields
    if payload.get("method_type") is None and not any(
        payload.get(k) is not None
        for k in ("handle", "address", "chain", "cashtag", "network_note", "instructions")
    ):
        fields = {}
        if payload.get("name"):
            fields["name"] = str(payload.get("name") or "").strip()[:60]
        if payload.get("instructions") is not None:
            fields["instructions"] = str(payload.get("instructions") or "").strip()[:1000]
        if payload.get("active") is not None:
            fields["active"] = 1 if payload["active"] in (1, True, "1", "true") else 0
    if not fields:
        return _err(400, "Nothing to update")
    db.update_payment_method(mid, **fields)
    return 200, {"ok": True, "id": mid, "created": False}


def ensure_shop_payments(
    chat_id: int,
    defaults: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Idempotently seed payment methods on a shop (by method_type).

    Existing methods of the same type are left alone so vendors can edit freely.
    """
    existing = db.list_payment_methods(int(chat_id), active_only=False)
    by_type = {
        (m.get("method_type") or "").lower(): m
        for m in existing
        if (m.get("method_type") or "").strip()
    }
    created: list[str] = []
    for item in defaults or []:
        mt = str(item.get("method_type") or "").lower()
        if not mt or mt in by_type:
            continue
        rendered = payment_templates.render_from_fields(
            mt,
            handle=str(item.get("handle") or ""),
            address=str(item.get("address") or ""),
            chain=str(item.get("chain") or ""),
            network_note=str(item.get("network_note") or ""),
            cashtag=str(item.get("cashtag") or ""),
            name=str(item.get("name") or ""),
            instructions=str(item.get("instructions") or ""),
        )
        db.add_payment_from_template(int(chat_id), rendered)
        created.append(mt)
        by_type[mt] = rendered
    return {"ok": True, "created": created, "total": len(by_type)}


def api_shipping(tok: dict, payload: dict) -> tuple[int, dict]:
    fields: dict[str, Any] = {}
    if payload.get("enabled") is not None:
        fields["shipping_enabled"] = (
            1 if payload["enabled"] in (1, True, "1", "true") else 0
        )
    for src, dst in (("fee", "shipping_fee"), ("free_above", "free_shipping_above")):
        if payload.get(src) is not None:
            try:
                v = float(payload[src])
            except (TypeError, ValueError):
                return _err(400, f"Bad {src}")
            if v < 0 or v > 100_000:
                return _err(400, f"Bad {src}")
            fields[dst] = v
    if not fields:
        return _err(400, "Nothing to update")
    db.update_shop(tok["chat_id"], **fields)
    return 200, {"ok": True}


def api_shop(tok: dict, payload: dict) -> tuple[int, dict]:
    fields: dict[str, Any] = {}
    if payload.get("title") is not None:
        title = " ".join(str(payload["title"]).split())[:80]
        if not title:
            return _err(400, "Title cannot be empty")
        fields["title"] = title
    if payload.get("welcome_text") is not None:
        fields["welcome_text"] = str(payload["welcome_text"]).strip()[:500] or None
    if not fields:
        return _err(400, "Nothing to update")
    db.update_shop(tok["chat_id"], **fields)
    return 200, {"ok": True}


# ── Orders (panel) ───────────────────────────────────────────────────────────

_ACTIONABLE_STATUSES = ("pending_payment", "awaiting_confirmation")
_TRACKING_URLS = {
    "ups": "https://www.ups.com/track?tracknum={tn}",
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={tn}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={tn}",
    "dhl": "https://www.dhl.com/en/express/tracking.html?AWB={tn}",
}


def tracking_url(carrier: str | None, tracking_number: str | None) -> str | None:
    """Known-carrier tracking page URL, or None."""
    tn = urllib.parse.quote((tracking_number or "").strip())
    if not tn:
        return None
    key = re.sub(r"[^a-z]", "", (carrier or "").strip().lower())
    tmpl = _TRACKING_URLS.get(key)
    if not tmpl:
        return None
    return tmpl.format(tn=tn)


def telegram_send_with_token(
    bot_token: str,
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str | None = "HTML",
) -> bool:
    """POST sendMessage with an explicit bot token (vendor storefront bot).

    Mirrors spbc_notify._telegram_api but never uses the main SPBC token —
    customers only started the vendor bot.
    """
    import urllib.error
    import urllib.request

    token = (bot_token or "").strip()
    if not token or not chat_id:
        return False
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text if len(text) <= 4000 else text[:3990] + "\n…",
        "disable_web_page_preview": False,
    }
    if parse_mode:
        body["parse_mode"] = parse_mode
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = json.loads(res.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            log.warning(
                "customer DM telegram HTTP %s: %s",
                exc.code,
                payload.get("description") or payload,
            )
        except Exception:
            log.warning("customer DM telegram HTTP %s", exc.code)
        return False
    except Exception as exc:
        log.warning("customer DM send failed: %s", exc)
        return False


def notify_order_customer(
    shop_chat_id: int, customer_user_id: int, text: str
) -> bool:
    """DM the buyer via their vendor storefront bot. Never the main SPBC bot."""
    try:
        import vendor_stores
    except Exception as exc:
        log.warning("vendor_stores import failed for customer DM: %s", exc)
        return False
    token = vendor_stores.get_bot_token_for_shop(int(shop_chat_id))
    if not token:
        log.warning(
            "no vendor bot token for shop %s — customer %s not notified",
            shop_chat_id,
            customer_user_id,
        )
        return False
    ok = telegram_send_with_token(token, int(customer_user_id), text)
    if not ok:
        log.warning(
            "customer DM failed shop=%s user=%s", shop_chat_id, customer_user_id
        )
    return ok


def _order_belongs(order: dict | None, shop_chat_id: int) -> bool:
    if not order:
        return False
    return int(order.get("chat_id") or 0) == int(shop_chat_id)


def _item_summary(items: list[dict]) -> str:
    parts = []
    for it in items:
        name = (it.get("product_name") or "item").strip()
        qty = int(it.get("quantity") or 0)
        parts.append(f"{name} × {qty}")
    return ", ".join(parts) if parts else "—"


def _order_public(order: dict) -> dict:
    oid = int(order["id"])
    items = db.get_order_items(oid)
    uname = (order.get("username") or "").strip()
    full = (order.get("full_name") or "").strip()
    return {
        "id": oid,
        "payment_code": order.get("payment_code") or "",
        "customer": {
            "user_id": int(order.get("user_id") or 0),
            "username": uname,
            "full_name": full,
            "display": full
            or (f"@{uname}" if uname else f"id:{order.get('user_id') or '?'}"),
        },
        "items_summary": _item_summary(items),
        "items": [
            {
                "name": it.get("product_name") or "",
                "quantity": int(it.get("quantity") or 0),
                "unit_price": float(it.get("unit_price") or 0),
                "line_total": float(it.get("line_total") or 0),
            }
            for it in items
        ],
        "subtotal": float(order.get("subtotal") or 0),
        "shipping_fee": float(order.get("shipping_fee") or 0),
        "total": float(order.get("total") or 0),
        "status": order.get("status") or "",
        "created_at": order.get("created_at") or "",
        "tracking_number": (order.get("tracking_number") or "").strip(),
        "tracking_carrier": (order.get("tracking_carrier") or "").strip(),
        "tracking_url": tracking_url(
            order.get("tracking_carrier"), order.get("tracking_number")
        ),
        "ship_name": (order.get("ship_name") or "").strip(),
        "ship_address": (order.get("ship_address") or "").strip(),
        "ship_notes": (order.get("ship_notes") or "").strip(),
    }


def api_orders(tok: dict, payload: dict | None = None) -> tuple[int, dict]:
    """List this shop's orders: actionable first, then recent paid/shipped."""
    chat_id = int(tok["chat_id"])
    # Pull enough rows; group in Python so actionable always surfaces first
    rows = db.list_orders(chat_id, status=None, limit=80)
    actionable: list[dict] = []
    rest: list[dict] = []
    for o in rows:
        st = str(o.get("status") or "")
        if st in _ACTIONABLE_STATUSES:
            actionable.append(o)
        elif st in ("paid", "shipped", "complete"):
            rest.append(o)
        # cancelled/rejected still useful briefly — keep a few
        elif st in ("cancelled", "rejected"):
            rest.append(o)
    # list_orders is already newest-first (id DESC)
    combined = actionable + rest
    return 200, {
        "ok": True,
        "orders": [_order_public(o) for o in combined],
    }


def api_confirm_payment(tok: dict, payload: dict) -> tuple[int, dict]:
    chat_id = int(tok["chat_id"])
    try:
        order_id = int(payload.get("order_id"))
    except (TypeError, ValueError):
        return _err(400, "Bad order id")
    order = db.get_order(order_id)
    if not _order_belongs(order, chat_id):
        return _err(404, "Order not found in your shop")
    ok, msg, _alerts = db.confirm_order_payment(order_id, int(tok["user_id"]))
    if not ok:
        return _err(400, msg or "Could not confirm payment")
    order = db.get_order(order_id) or order
    code = (order.get("payment_code") or str(order_id)).strip()
    text = (
        f"✅ Payment received for order <code>{code}</code>! "
        "It's being prepared — you'll get tracking here when it ships."
    )
    notified = notify_order_customer(chat_id, int(order["user_id"]), text)
    return 200, {
        "ok": True,
        "status": order.get("status") or "paid",
        "message": msg,
        "customer_notified": bool(notified),
    }


def api_set_tracking(tok: dict, payload: dict) -> tuple[int, dict]:
    chat_id = int(tok["chat_id"])
    try:
        order_id = int(payload.get("order_id"))
    except (TypeError, ValueError):
        return _err(400, "Bad order id")
    carrier = str(payload.get("carrier") or "").strip()
    tracking_number = str(payload.get("tracking_number") or "").strip()
    if not tracking_number:
        return _err(400, "Tracking number required")
    order = db.get_order(order_id)
    if not _order_belongs(order, chat_id):
        return _err(404, "Order not found in your shop")
    status = str(order.get("status") or "")
    if status not in ("paid", "shipped", "complete"):
        return _err(
            400,
            f"Order must be paid before shipping (status: {status or 'unknown'}).",
        )
    if not db.set_order_tracking(order_id, tracking_number, carrier or None):
        return _err(400, "Could not save tracking")
    if status == "paid":
        ship_ok, ship_msg = db.mark_order_shipped(order_id)
        if not ship_ok and "already" not in (ship_msg or "").lower():
            # Tracking is already saved; still report the ship-mark failure
            log.warning(
                "set_tracking: mark_order_shipped failed order=%s: %s",
                order_id,
                ship_msg,
            )
            return _err(400, ship_msg or "Could not mark shipped")
    order = db.get_order(order_id) or order
    code = (order.get("payment_code") or str(order_id)).strip()
    car = (order.get("tracking_carrier") or carrier or "").strip() or "—"
    tn = (order.get("tracking_number") or tracking_number).strip()
    url = tracking_url(car if car != "—" else None, tn)
    text = (
        f"📦 Your order <code>{code}</code> has shipped! "
        f"Carrier: {car} · Tracking: {tn}"
    )
    if url:
        text += f"\n🔗 {url}"
    notified = notify_order_customer(chat_id, int(order["user_id"]), text)
    return 200, {
        "ok": True,
        "status": order.get("status") or "shipped",
        "tracking_number": tn,
        "tracking_carrier": "" if car == "—" else car,
        "tracking_url": url,
        "customer_notified": bool(notified),
    }


def _parse_ymd(s: str | None) -> str | None:
    raw = (s or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    return raw


def _created_day(created_at: str | None) -> str:
    """First 10 chars of created_at (YYYY-MM-DD) for inclusive date compare."""
    s = (created_at or "").strip()
    return s[:10] if len(s) >= 10 else s


def api_order_history_txt(
    tok: dict,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[str, str]:
    """Plain-text order export for the shop. Returns (text, filename)."""
    chat_id = int(tok["chat_id"])
    today = _utc_now().date()
    end_s = _parse_ymd(end_date) or today.isoformat()
    start_s = _parse_ymd(start_date) or (today - timedelta(days=30)).isoformat()
    if start_s > end_s:
        start_s, end_s = end_s, start_s

    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE chat_id = ?
              AND substr(created_at, 1, 10) >= ?
              AND substr(created_at, 1, 10) <= ?
            ORDER BY id DESC
            """,
            (chat_id, start_s, end_s),
        ).fetchall()
        orders = [dict(r) for r in rows]

    blocks: list[str] = []
    header = (
        f"Order history — shop {chat_id}\n"
        f"Range: {start_s} to {end_s} (inclusive)\n"
        f"Orders: {len(orders)}\n"
        f"{'=' * 48}"
    )
    blocks.append(header)
    for o in orders:
        items = db.get_order_items(int(o["id"]))
        uname = (o.get("username") or "").strip()
        full = (o.get("full_name") or "").strip()
        cust = full or (f"@{uname}" if uname else f"user_id={o.get('user_id')}")
        if full and uname:
            cust = f"{full} (@{uname})"
        lines = [
            "",
            f"Date:    {o.get('created_at') or '—'}",
            f"Code:    {o.get('payment_code') or '—'}",
            f"Status:  {o.get('status') or '—'}",
            f"Customer:{cust}",
            "Items:",
        ]
        for it in items:
            name = it.get("product_name") or "item"
            qty = int(it.get("quantity") or 0)
            lt = float(it.get("line_total") or 0)
            lines.append(f"  · {name} × {qty} — ${lt:,.2f}")
        if not items:
            lines.append("  (no line items)")
        lines.append(f"Shipping: ${float(o.get('shipping_fee') or 0):,.2f}")
        lines.append(f"Total:    ${float(o.get('total') or 0):,.2f}")
        sn = (o.get("ship_name") or "").strip()
        sa = (o.get("ship_address") or "").strip().replace("\n", ", ")
        snotes = (o.get("ship_notes") or "").strip()
        ship_bits = [b for b in (sn, sa, snotes) if b]
        if ship_bits:
            lines.append("Ship to: " + " · ".join(ship_bits))
        else:
            lines.append("Ship to: —")
        tn = (o.get("tracking_number") or "").strip()
        if tn:
            car = (o.get("tracking_carrier") or "").strip()
            lines.append(f"Tracking: {car + ' · ' if car else ''}{tn}")
        lines.append("-" * 48)
        blocks.append("\n".join(lines))

    text = "\n".join(blocks) + "\n"
    filename = f"orders_{chat_id}_{start_s}_{end_s}.txt"
    return text, filename


_API_POST = {
    "product": api_product,
    "media": api_media,
    "restock": api_restock,
    "bulk": api_bulk,
    "payment": api_payment,
    "shipping": api_shipping,
    "shop": api_shop,
    "orders": api_orders,
    "confirm_payment": api_confirm_payment,
    "set_tracking": api_set_tracking,
}


# ── HTTP layer (called from spbc_notify's request handler) ──────────────────

def handle_panel_get(path: str, query: dict) -> tuple[int, str, bytes]:
    """Returns (status, content_type, body).

    For attachment downloads, content_type may be prefixed with a disposition
    hint encoded as ``text/plain; charset=utf-8`` and the caller may set
    Content-Disposition separately (see spbc_notify /panel/api/orders.txt).
    """
    if path == "/panel":
        return 200, "text/html; charset=utf-8", PANEL_HTML.encode("utf-8")
    if path == "/panel/api/state":
        tok = resolve_token((query.get("t") or [""])[0])
        if not tok:
            body = json.dumps({"ok": False, "error": "invalid_or_expired_link"})
            return 401, "application/json", body.encode("utf-8")
        code, data = api_state(tok)
        return code, "application/json", json.dumps(data).encode("utf-8")
    if path == "/panel/api/orders.txt":
        tok = resolve_token((query.get("t") or [""])[0])
        if not tok:
            body = json.dumps({"ok": False, "error": "invalid_or_expired_link"})
            return 401, "application/json", body.encode("utf-8")
        start = (query.get("start") or [""])[0]
        end = (query.get("end") or [""])[0]
        text, filename = api_order_history_txt(tok, start, end)
        # filename stashed after a NUL for the HTTP layer (orders.txt route)
        body = text.encode("utf-8")
        # content type + special marker so spbc_notify can attach Content-Disposition
        ctype = f"text/plain; charset=utf-8; name={filename}"
        return 200, ctype, body
    return 404, "application/json", b'{"error":"not_found"}'


def handle_panel_post(path: str, payload: dict) -> tuple[int, str, bytes]:
    name = path.removeprefix("/panel/api/")
    fn = _API_POST.get(name)
    if fn is None:
        return 404, "application/json", b'{"error":"not_found"}'
    tok = resolve_token(str(payload.get("t") or ""))
    if not tok:
        body = json.dumps({"ok": False, "error": "invalid_or_expired_link"})
        return 401, "application/json", body.encode("utf-8")
    try:
        code, data = fn(tok, payload)
    except Exception as exc:
        log.error("panel api %s failed: %s", name, exc, exc_info=exc)
        code, data = 500, {"ok": False, "error": "server_error"}
    return code, "application/json", json.dumps(data).encode("utf-8")


# ── The page ─────────────────────────────────────────────────────────────────

PANEL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Shop Panel</title>
<style>
  :root{--bg:#f5f6f8;--card:#fff;--ink:#16202b;--mut:#66707c;--line:#e3e6ea;
        --go:#1a7f4e;--warn:#b4231f;--accent:#245fa6}
  @media(prefers-color-scheme:dark){:root{--bg:#10151b;--card:#1a212a;
        --ink:#e8ecf1;--mut:#93a0ad;--line:#2a333e;--accent:#6aa5e8}}
  *{box-sizing:border-box}
  body{margin:0;font:16px/1.45 system-ui,Segoe UI,Roboto,sans-serif;
       background:var(--bg);color:var(--ink);padding:12px;max-width:760px;
       margin-inline:auto}
  h1{font-size:1.25rem;margin:8px 4px}
  h2{font-size:1rem;margin:0 0 10px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:14px;margin-bottom:14px}
  input,textarea,select{font:inherit;color:inherit;background:transparent;
        border:1px solid var(--line);border-radius:8px;padding:7px 9px;
        width:100%}
  input:focus,textarea:focus{outline:2px solid var(--accent);border-color:transparent}
  label{font-size:.8rem;color:var(--mut);display:block;margin-bottom:2px}
  button{font:inherit;border:0;border-radius:9px;padding:9px 14px;
         background:var(--accent);color:#fff;cursor:pointer}
  button.sub{background:transparent;color:var(--accent);border:1px solid var(--line)}
  button.danger{background:transparent;color:var(--warn);border:1px solid var(--line)}
  .row{display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin-bottom:10px}
  .row>div{flex:1;min-width:70px}
  .row .name{flex:2.5;min-width:140px}
  .row .num{max-width:92px}
  .prod{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}
  .off{opacity:.55}
  .tag{font-size:.72rem;color:var(--mut)}
  .msg{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);
       background:var(--ink);color:var(--bg);padding:9px 16px;border-radius:10px;
       opacity:0;transition:opacity .25s;pointer-events:none;max-width:90vw}
  .msg.show{opacity:1}
  .flex{display:flex;gap:8px;align-items:center}
  .grow{flex:1}
  textarea{min-height:110px;font-family:ui-monospace,Consolas,monospace;
           font-size:.85rem}
  .low{color:var(--warn);font-weight:600}
  .thumb{width:44px;height:44px;object-fit:cover;border-radius:8px;
         border:1px solid var(--line)}
  .media{margin-top:8px;flex-wrap:wrap}
  .media button{padding:6px 10px;font-size:.85rem}
  .media a{color:var(--accent)}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 4px 12px}
  .tabs button{background:transparent;color:var(--ink);border:1px solid var(--line);
               padding:8px 12px;border-radius:999px;font-size:.9rem}
  .tabs button.on{background:var(--accent);color:#fff;border-color:transparent}
  .status{display:inline-block;font-size:.72rem;padding:2px 8px;border-radius:999px;
          border:1px solid var(--line);color:var(--mut)}
  .status.act{color:var(--go);border-color:var(--go)}
  .status.warn{color:#a15c00;border-color:#e0b46a}
  .ord{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}
  .ord:first-child{border-top:0;padding-top:0;margin-top:0}
  .ord h3{font-size:.95rem;margin:0 0 4px}
  .hide{display:none!important}
</style>
</head>
<body>
<h1 id="title">Shop Panel</h1>
<div id="app"><div class="card">Loading… If this never loads, your link
expired — send <b>/webpanel</b> to the bot for a fresh one.</div></div>
<div class="msg" id="msg"></div>
<script>
// Keep the token out of the visible URL (browser history / shoulder surfing).
// It stays in sessionStorage for reloads; the emailed link always re-supplies it.
const Q=new URLSearchParams(location.search);
let T=Q.get('t')||'';
const MODE=Q.get('mode')||'';
try{
  if(T){sessionStorage.setItem('spbc_panel_t',T);
    history.replaceState(null,'',location.pathname);}
  else{T=sessionStorage.getItem('spbc_panel_t')||'';}
}catch(e){/* private mode: token simply stays in the URL */}
const $=s=>document.querySelector(s);
let S=null;
let ORDERS=[];
let TAB=(MODE==='restock')?'catalog':'orders';
function toast(t,bad){const m=$('#msg');m.textContent=t;
  m.style.background=bad?'#b4231f':'';m.classList.add('show');
  setTimeout(()=>m.classList.remove('show'),2600);}
async function api(name,body){
  const r=await fetch('panel/api/'+name,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({t:T},body||{}))});
  const d=await r.json().catch(()=>({ok:false,error:'network'}));
  if(!d.ok)toast(d.error||'Failed',true);
  return d;}
async function loadOrders(){
  const d=await api('orders',{});
  if(d&&d.ok)ORDERS=d.orders||[];
  return d;}
async function load(){
  const r=await fetch('panel/api/state?t='+encodeURIComponent(T));
  const d=await r.json().catch(()=>null);
  if(!d||!d.ok){$('#app').innerHTML='<div class="card">This link is invalid '+
    'or expired.<br><br>Open Telegram and send <b>/webpanel</b> to the bot '+
    'to get a fresh link.</div>';return;}
  S=d;
  await loadOrders();
  render();}
function esc(x){const d=document.createElement('div');
  d.textContent=x==null?'':String(x);return d.innerHTML;}
function money(n){const x=Number(n||0);return '$'+x.toFixed(2);}
function statusClass(st){
  if(st==='awaiting_confirmation'||st==='pending_payment')return 'act';
  if(st==='paid')return 'warn';
  return '';}
function ymd(d){return d.toISOString().slice(0,10);}
function defaultRange(){
  const end=new Date();const start=new Date();start.setDate(end.getDate()-30);
  return {start:ymd(start),end:ymd(end)};}
function orderCard(o){
  const st=o.status||'';
  const canConfirm=st==='pending_payment'||st==='awaiting_confirmation';
  const canShip=st==='paid'||st==='shipped'||st==='awaiting_confirmation';
  // tracking only after paid (server also enforces)
  const showTrack=st==='paid'||st==='shipped'||st==='complete';
  const cust=esc((o.customer&&o.customer.display)||'—');
  const code=esc(o.payment_code||('#'+o.id));
  return `<div class="ord" data-oid="${o.id}">
    <div class="flex" style="align-items:flex-start;gap:8px;flex-wrap:wrap">
      <div class="grow">
        <h3><code>${code}</code>
          <span class="status ${statusClass(st)}">${esc(st)}</span></h3>
        <div class="tag">${esc(o.created_at||'')} · ${cust}</div>
        <div style="margin:4px 0">${esc(o.items_summary||'—')}</div>
        <div><b>${money(o.total)}</b>
          ${o.shipping_fee?`<span class="tag">ship ${money(o.shipping_fee)}</span>`:''}
        </div>
      </div>
      ${canConfirm?`<button class="b-confirm" data-oid="${o.id}">Confirm payment</button>`:''}
    </div>
    ${showTrack?`<div class="row" style="margin-top:10px">
      <div class="num" style="max-width:120px"><label>Carrier</label>
        <input class="f-carrier" list="carriers" value="${esc(o.tracking_carrier||'')}"
          placeholder="USPS"></div>
      <div class="name"><label>Tracking #</label>
        <input class="f-track" value="${esc(o.tracking_number||'')}"
          placeholder="9400…"></div>
      <div><button class="sub b-ship" data-oid="${o.id}">Save &amp; notify customer</button></div>
    </div>`:''}
    ${o.tracking_number&&!showTrack?`<div class="tag">Tracking: ${esc(o.tracking_carrier||'')} ${esc(o.tracking_number)}</div>`:''}
  </div>`;}
function prodRow(p){
  const low=p.active&&p.stock<=2?' low':'';
  const thumb=p.photo_url
    ? `<img class="thumb" src="${esc(p.photo_url)}" alt="">`
    : (p.has_photo?'<span class="tag">photo set in Telegram</span>':'');
  const coaBit=p.coa_url
    ? `<a href="${esc(p.coa_url)}" target="_blank" rel="noopener">COA ✓</a>`
    : (p.has_coa_file?'<span class="tag">COA file in Telegram</span>':'<span class="tag">no COA</span>');
  return `<div class="prod${p.active?'':' off'}" data-id="${p.id}">
    <div class="row">
      <div class="name"><label>Product</label>
        <input class="f-name" value="${esc(p.name)}"></div>
      <div class="num"><label>Price</label>
        <input class="f-price" type="number" step="0.01" min="0" value="${p.price}"></div>
      <div class="num"><label>Kit price</label>
        <input class="f-kit" type="number" step="0.01" min="0" value="${p.kit_price==null?'':p.kit_price}"></div>
      <div class="num"><label>Stock</label>
        <input class="f-stock${low}" type="number" step="1" min="0" value="${p.stock}"></div>
    </div>
    <div class="flex media">
      ${thumb}
      <button class="sub b-photo">📷 ${p.has_photo?'Change':'Add'} photo</button>
      ${p.has_photo?'<button class="danger b-photox">✕</button>':''}
      <span class="grow"></span>
    </div>
    <div class="flex media">
      <span class="tag">COA:</span> ${coaBit}
      <button class="sub b-coafile">📄 Upload PDF</button>
      <button class="sub b-coalink">🔗 Link</button>
      ${(p.coa_url||p.has_coa_file)?'<button class="danger b-coax">✕</button>':''}
    </div>
    <div class="flex">
      <label class="flex" style="margin:0"><input type="checkbox" class="f-act"
        style="width:auto" ${p.active?'checked':''}> for sale</label>
      <span class="tag grow">${p.site_key?'linked from your website':''}</span>
      <button class="sub b-save">Save</button>
    </div></div>`;}
function render(){
  $('#title').textContent=S.shop.title;
  const sh=S.shop;
  const range=defaultRange();
  const actionable=ORDERS.filter(o=>o.status==='pending_payment'||o.status==='awaiting_confirmation').length;
  $('#app').innerHTML=`
  <div class="tabs">
    <button type="button" data-tab="orders" class="${TAB==='orders'?'on':''}">
      Orders${actionable?` (${actionable})`:''}</button>
    <button type="button" data-tab="catalog" class="${TAB==='catalog'?'on':''}">
      Catalog</button>
    <button type="button" data-tab="settings" class="${TAB==='settings'?'on':''}">
      Settings</button>
  </div>
  <div id="tab-orders" class="${TAB==='orders'?'':'hide'}">
  <div class="card"><h2>Orders</h2>
    <p class="tag" style="margin:0 0 10px">Confirm payments and add tracking.
      The customer is messaged on your storefront bot automatically.</p>
    <datalist id="carriers">
      <option value="USPS"><option value="UPS"><option value="FedEx"><option value="DHL">
    </datalist>
    <div id="olist">${ORDERS.length?ORDERS.map(orderCard).join(''):
      '<p class="tag">No orders yet.</p>'}</div>
    <div class="flex" style="margin-top:12px">
      <button class="sub" id="ord-refresh">Refresh</button>
      <span class="grow"></span>
    </div>
  </div>
  <div class="card"><h2>Export history</h2>
    <div class="row">
      <div class="num" style="max-width:160px"><label>From</label>
        <input id="ex-start" type="date" value="${range.start}"></div>
      <div class="num" style="max-width:160px"><label>To</label>
        <input id="ex-end" type="date" value="${range.end}"></div>
      <div><button class="sub" id="ex-go">Download .txt</button></div>
    </div>
    <span class="tag">One readable block per order in the date range.</span>
  </div>
  </div>
  <div id="tab-catalog" class="${TAB==='catalog'?'':'hide'}">
  <div class="card"><h2>Products (${S.products.length})</h2>
    <div class="flex" style="margin-bottom:10px">
      <button id="restock-on">📦 Received a shipment</button>
      <span class="tag grow">Adds to stock instead of replacing it</span>
    </div>
    <div id="restock" style="display:none">
      <div class="tag" style="margin-bottom:6px">Type how many arrived of each
        item, then Apply. Blank = unchanged.</div>
      ${S.products.map(p=>`<div class="row rs" data-id="${p.id}">
        <div class="name"><label>${esc(p.name)}</label>
          <span class="tag">now: ${p.stock}</span></div>
        <div class="num"><label>+ arrived</label>
          <input class="rs-add" type="number" step="1" min="0" placeholder="0"></div>
      </div>`).join('')}
      <div class="flex">
        <button class="sub" id="restock-off">Cancel</button>
        <span class="grow"></span>
        <button id="restock-go">✅ Apply shipment</button>
      </div>
    </div>
    <div id="plist">${S.products.map(prodRow).join('')}</div>
    <div class="prod"><div class="row">
      <div class="name"><label>New product</label><input id="np-name" placeholder="BPC-157 10MG"></div>
      <div class="num"><label>Price / vial</label><input id="np-price" type="number" step="0.01" min="0"></div>
      <div class="num"><label>Kit price</label><input id="np-kit" type="number" step="0.01" min="0" placeholder="—"></div>
      <div class="num"><label>Stock</label><input id="np-stock" type="number" step="1" min="0" value="0"></div>
      <div><button id="np-add">Add</button></div></div>
      <span class="tag">Kit price optional — leave blank to sell vials only.</span></div>
    <div class="prod"><label>Bulk add / update — one per line:
      name | price | stock | kit:PRICE</label>
      <textarea id="bulk" placeholder="BPC-157 10MG | 41 | 10 | kit:294&#10;TB-500 10MG | 47 | 5"></textarea>
      <div class="flex" style="margin-top:8px">
        <span class="tag grow">Existing names update; new names are created.
          Add <b>kit:294</b> for kit pricing.</span>
        <button class="sub" id="bulk-go">Import</button></div></div>
  </div>
  <div class="card"><h2>Payment methods</h2>
    <p class="tag" style="margin:0 0 10px">Buyers see these at checkout. Edit anytime and hit Save — changes apply immediately.</p>
    <div id="paylist">${S.payments.map(m=>{
      const t=m.method_type||'custom';
      const handle=esc(m.handle||m.cashtag||'');
      const addr=esc(m.address||'');
      const chain=esc(m.chain||'');
      const note=esc(m.network_note||'');
      return `<div class="prod${m.active?'':' off'}" data-mid="${m.id}" data-type="${esc(t)}">
        <div class="row">
          <div class="name"><label>Type</label>
            <select class="p-type">
              <option value="venmo"${t==='venmo'?' selected':''}>Venmo</option>
              <option value="paypal"${t==='paypal'?' selected':''}>PayPal</option>
              <option value="zelle"${t==='zelle'?' selected':''}>Zelle</option>
              <option value="apple_cash"${t==='apple_cash'?' selected':''}>Apple Cash</option>
              <option value="crypto"${t==='crypto'?' selected':''}>Crypto</option>
              <option value="cashapp"${t==='cashapp'?' selected':''}>Cash App</option>
              <option value="custom"${t==='custom'?' selected':''}>Custom</option>
            </select></div>
          <div class="name"><label>Label</label>
            <input class="p-name" value="${esc(m.name)}"></div></div>
        <div class="row p-fields">
          <div class="name p-f-handle"><label>Handle / email / phone</label>
            <input class="p-handle" value="${handle}" placeholder="@handle, email, or phone"></div>
          <div class="name p-f-chain" style="${t==='crypto'?'':'display:none'}"><label>Coin</label>
            <input class="p-chain" value="${chain}" placeholder="BTC / ETH / USDT"></div>
          <div class="name p-f-addr" style="${t==='crypto'?'':'display:none'}"><label>Wallet address</label>
            <input class="p-addr" value="${addr}" placeholder="0x… or bc1…"></div>
          <div class="name p-f-note" style="${t==='crypto'||t==='paypal'?'':'display:none'}"><label>${t==='paypal'?'PayPal mode':'Network note'}</label>
            <input class="p-note" value="${note}" placeholder="${t==='paypal'?'friends_family':'e.g. USDT TRC20'}"></div>
        </div>
        <label>Instructions shown to buyers (auto-filled from the fields above — editable)</label>
        <textarea class="p-instr" style="min-height:60px">${esc(m.instructions)}</textarea>
        <div class="flex" style="margin-top:8px">
          <label class="flex" style="margin:0"><input type="checkbox" class="p-act"
            style="width:auto" ${m.active?'checked':''}> enabled</label>
          <span class="grow"></span>
          <button class="danger p-del">Delete</button>
          <button class="sub p-save">Save changes</button></div></div>`;}).join('')||'<p class="tag">No payment methods yet — add one below.</p>'}
    <div class="prod">
      <label>Quick-add a method</label>
      <div class="flex" style="flex-wrap:wrap;gap:8px;margin:8px 0">
        <button type="button" class="sub pm-quick" data-type="venmo">+ Venmo</button>
        <button type="button" class="sub pm-quick" data-type="paypal">+ PayPal</button>
        <button type="button" class="sub pm-quick" data-type="zelle">+ Zelle</button>
        <button type="button" class="sub pm-quick" data-type="apple_cash">+ Apple Cash</button>
        <button type="button" class="sub pm-quick" data-type="crypto">+ Crypto</button>
        <button type="button" class="sub pm-quick" data-type="cashapp">+ Cash App</button>
      </div>
      <div class="row">
        <div class="name"><label>Or custom label</label>
          <input id="pm-name" placeholder="Custom payment name"></div>
        <div><button id="pm-add">Add custom</button></div></div>
      <span class="tag">You can change any method anytime — just edit and Save.</span>
    </div>
  </div>
  </div>
  <div id="tab-settings" class="${TAB==='settings'?'':'hide'}">
  <div class="card"><h2>Shop</h2>
    <div class="row"><div class="name"><label>Shop name</label>
      <input id="shop-title" value="${esc(sh.title)}"></div>
      <div><button class="sub" id="shop-save">Save</button></div></div>
  </div>
  <div class="card"><h2>Shipping</h2>
    <div class="row">
      <div><label>&nbsp;</label><label class="flex" style="margin:0">
        <input type="checkbox" id="sh-on" style="width:auto"
        ${sh.shipping_enabled?'checked':''}> charge shipping</label></div>
      <div class="num"><label>Fee</label>
        <input id="sh-fee" type="number" step="0.01" min="0" value="${sh.shipping_fee}"></div>
      <div class="num"><label>Free over</label>
        <input id="sh-free" type="number" step="0.01" min="0" value="${sh.free_shipping_above}"></div>
      <div><button class="sub" id="sh-save">Save</button></div></div>
    <span class="tag">Changes apply to new checkouts immediately.</span>
  </div>
  </div>`;
  wire();}
// Read a file, shrinking images client-side so phone photos upload fast
function pickFile(accept,resize){
  return new Promise(res=>{
    const inp=document.createElement('input');
    inp.type='file';inp.accept=accept;
    inp.onchange=()=>{
      const f=inp.files&&inp.files[0];
      if(!f)return res(null);
      if(f.size>6*1024*1024&&!resize)
        {toast('File too big (max 6 MB)',true);return res(null);}
      const fr=new FileReader();
      fr.onload=()=>{
        if(!resize)return res(fr.result);
        const img=new Image();
        img.onload=()=>{
          const max=1280;
          let{width:w,height:h}=img;
          if(w>max||h>max){const s=Math.min(max/w,max/h);w=Math.round(w*s);h=Math.round(h*s);}
          const c=document.createElement('canvas');c.width=w;c.height=h;
          c.getContext('2d').drawImage(img,0,0,w,h);
          res(c.toDataURL('image/jpeg',0.82));
        };
        img.onerror=()=>res(fr.result);
        img.src=fr.result;
      };
      fr.readAsDataURL(f);
    };
    inp.click();
  });
}
async function media(body){const d=await api('media',body);if(d.ok)load();return d;}

function wire(){
  document.querySelectorAll('.tabs button').forEach(btn=>{
    btn.onclick=()=>{TAB=btn.dataset.tab;render();};});
  const ordRefresh=$('#ord-refresh');
  if(ordRefresh)ordRefresh.onclick=async()=>{
    toast('Refreshing…');await loadOrders();render();};
  document.querySelectorAll('.b-confirm').forEach(btn=>{
    btn.onclick=async()=>{
      if(!confirm('Mark this payment as received and deduct stock?'))return;
      const d=await api('confirm_payment',{order_id:btn.dataset.oid});
      if(!d.ok)return;
      const note=d.customer_notified===false
        ? 'Payment confirmed (could not message customer)'
        : 'Payment confirmed — customer notified';
      toast(note);await loadOrders();render();};});
  document.querySelectorAll('.b-ship').forEach(btn=>{
    btn.onclick=async()=>{
      const card=btn.closest('.ord');
      const carrier=(card.querySelector('.f-carrier')||{}).value||'';
      const tracking_number=(card.querySelector('.f-track')||{}).value||'';
      if(!String(tracking_number).trim()){toast('Tracking number required',true);return;}
      const d=await api('set_tracking',{order_id:btn.dataset.oid,carrier,tracking_number});
      if(!d.ok)return;
      const note=d.customer_notified===false
        ? 'Saved, but could not message the customer'
        : 'Shipped — customer notified with tracking';
      toast(note);await loadOrders();render();};});
  const exGo=$('#ex-go');
  if(exGo)exGo.onclick=()=>{
    const start=$('#ex-start').value||'';
    const end=$('#ex-end').value||'';
    const q=new URLSearchParams({t:T});
    if(start)q.set('start',start);
    if(end)q.set('end',end);
    // open attachment download (token in query; one-shot browser GET)
    window.location.href='panel/api/orders.txt?'+q.toString();
  };
  const rs=$('#restock'),pl=$('#plist');
  if(rs&&pl){
  const openRestock=()=>{rs.style.display='';pl.style.display='none';
    $('#restock-on').style.display='none';
    rs.scrollIntoView({behavior:'smooth',block:'start'});};
  $('#restock-on').onclick=openRestock;
  if(MODE==='restock'){TAB='catalog';openRestock();}
  $('#restock-off').onclick=()=>{rs.style.display='none';pl.style.display='';
    $('#restock-on').style.display='';};
  $('#restock-go').onclick=async()=>{
    const items=[...document.querySelectorAll('#restock .rs')].map(el=>({
      id:el.dataset.id, add:parseInt(el.querySelector('.rs-add').value||'0',10)
    })).filter(x=>x.add>0);
    if(!items.length){toast('Enter at least one quantity',true);return;}
    const d=await api('restock',{items});
    if(d.ok){toast(`Stock added to ${d.count} product${d.count===1?'':'s'} ✅`);
      load();}};
  }
  const shopSave=$('#shop-save');
  if(shopSave)shopSave.onclick=async()=>{
    const d=await api('shop',{title:$('#shop-title').value});
    if(d.ok){toast('Shop name saved');S.shop.title=$('#shop-title').value;
      $('#title').textContent=S.shop.title;}};
  document.querySelectorAll('#plist .prod').forEach(el=>{
    const id=el.dataset.id;
    el.querySelector('.b-photo').onclick=async()=>{
      const d=await pickFile('image/*',true);
      if(!d)return;
      toast('Uploading photo…');
      await media({id,kind:'photo',data_url:d});};
    const px=el.querySelector('.b-photox');
    if(px)px.onclick=async()=>{
      if(!confirm('Remove this photo?'))return;
      await media({id,kind:'photo',clear:true});};
    el.querySelector('.b-coafile').onclick=async()=>{
      const d=await pickFile('application/pdf,image/*',false);
      if(!d)return;
      toast('Uploading COA…');
      await media({id,kind:'coa',data_url:d});};
    el.querySelector('.b-coalink').onclick=async()=>{
      const u=prompt('Paste the COA link (https://...)');
      if(!u)return;
      await media({id,kind:'coa',url:u.trim()});};
    const cx=el.querySelector('.b-coax');
    if(cx)cx.onclick=async()=>{
      if(!confirm('Remove this COA?'))return;
      await media({id,kind:'coa',clear:true});};
    el.querySelector('.b-save').onclick=async()=>{
      const d=await api('product',{id:el.dataset.id,
        name:el.querySelector('.f-name').value,
        price:el.querySelector('.f-price').value,
        kit_price:el.querySelector('.f-kit').value||null,
        stock:el.querySelector('.f-stock').value,
        active:el.querySelector('.f-act').checked});
      if(d.ok)toast('Saved');};});
  $('#np-add').onclick=async()=>{
    const d=await api('product',{name:$('#np-name').value,
      price:$('#np-price').value,stock:$('#np-stock').value||0,
      kit_price:$('#np-kit').value||null});
    if(d.ok){toast('Product added');load();}};
  $('#bulk-go').onclick=async()=>{
    const d=await api('bulk',{text:$('#bulk').value});
    if(d.ok){toast(`Imported: ${d.created} new, ${d.updated} updated`+
      (d.errors.length?`, ${d.errors.length} errors`:''));load();}};
  document.querySelectorAll('#paylist .prod[data-mid]').forEach(el=>{
    const typeSel=el.querySelector('.p-type');
    const syncFields=()=>{
      const t=typeSel.value;
      el.querySelectorAll('.p-f-chain,.p-f-addr').forEach(n=>n.style.display=t==='crypto'?'':'none');
      el.querySelectorAll('.p-f-note').forEach(n=>n.style.display=(t==='crypto'||t==='paypal')?'':'none');
      el.querySelectorAll('.p-f-handle').forEach(n=>n.style.display=t==='crypto'?'none':'');
    };
    if(typeSel){typeSel.onchange=syncFields;syncFields();}
    el.querySelector('.p-save').onclick=async()=>{
      const d=await api('payment',{
        id:el.dataset.mid,
        method_type:typeSel?typeSel.value:'custom',
        name:el.querySelector('.p-name').value,
        handle:(el.querySelector('.p-handle')||{}).value||'',
        chain:(el.querySelector('.p-chain')||{}).value||'',
        address:(el.querySelector('.p-addr')||{}).value||'',
        network_note:(el.querySelector('.p-note')||{}).value||'',
        instructions:el.querySelector('.p-instr').value,
        active:el.querySelector('.p-act').checked});
      if(d.ok){toast('Payment method saved');load();}};
    el.querySelector('.p-del').onclick=async()=>{
      if(!confirm('Delete this payment method?'))return;
      const d=await api('payment',{id:el.dataset.mid,delete:true});
      if(d.ok){toast('Deleted');load();}};});
  document.querySelectorAll('.pm-quick').forEach(btn=>{
    btn.onclick=async()=>{
      const t=btn.dataset.type;
      const placeholders={
        venmo:'@yourhandle',
        paypal:'you@email.com',
        zelle:'email or phone',
        apple_cash:'phone number',
        cashapp:'$cashtag',
        crypto:''
      };
      let handle='', address='', chain='', network_note='';
      if(t==='crypto'){
        chain=prompt('Coin (BTC / ETH / USDT / Other)','USDT')||'';
        address=prompt('Wallet address','')||'';
        if(!address.trim()){toast('Address required',true);return;}
        network_note=prompt('Network note (optional)','')||'';
      } else {
        handle=prompt('Enter your '+(t.replace('_',' '))+ ' details', placeholders[t]||'')||'';
        if(!handle.trim()){toast('Value required',true);return;}
        if(t==='paypal') network_note='friends_family';
      }
      const d=await api('payment',{method_type:t,handle,address,chain,network_note});
      if(d.ok){toast('Added — edit anytime below');load();}
      else toast(d.error||'Could not add',true);
    };
  });
  $('#pm-add').onclick=async()=>{
    const name=$('#pm-name').value.trim();
    if(!name){toast('Name required',true);return;}
    const d=await api('payment',{method_type:'custom',name,instructions:''});
    if(d.ok){toast('Added — write instructions and Save');load();}};
  $('#sh-save').onclick=async()=>{
    const d=await api('shipping',{enabled:$('#sh-on').checked,
      fee:$('#sh-fee').value,free_above:$('#sh-free').value});
    if(d.ok)toast('Shipping saved');};}
load();
</script>
</body>
</html>
"""
