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


_API_POST = {
    "product": api_product,
    "media": api_media,
    "restock": api_restock,
    "bulk": api_bulk,
    "payment": api_payment,
    "shipping": api_shipping,
    "shop": api_shop,
}


# ── HTTP layer (called from spbc_notify's request handler) ──────────────────

def handle_panel_get(path: str, query: dict) -> tuple[int, str, bytes]:
    """Returns (status, content_type, body)."""
    if path == "/panel":
        return 200, "text/html; charset=utf-8", PANEL_HTML.encode("utf-8")
    if path == "/panel/api/state":
        tok = resolve_token((query.get("t") or [""])[0])
        if not tok:
            body = json.dumps({"ok": False, "error": "invalid_or_expired_link"})
            return 401, "application/json", body.encode("utf-8")
        code, data = api_state(tok)
        return code, "application/json", json.dumps(data).encode("utf-8")
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
function toast(t,bad){const m=$('#msg');m.textContent=t;
  m.style.background=bad?'#b4231f':'';m.classList.add('show');
  setTimeout(()=>m.classList.remove('show'),2600);}
async function api(name,body){
  const r=await fetch('panel/api/'+name,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({t:T},body))});
  const d=await r.json().catch(()=>({ok:false,error:'network'}));
  if(!d.ok)toast(d.error||'Failed',true);
  return d;}
async function load(){
  const r=await fetch('panel/api/state?t='+encodeURIComponent(T));
  const d=await r.json().catch(()=>null);
  if(!d||!d.ok){$('#app').innerHTML='<div class="card">This link is invalid '+
    'or expired.<br><br>Open Telegram and send <b>/webpanel</b> to the bot '+
    'to get a fresh link.</div>';return;}
  S=d;render();}
function esc(x){const d=document.createElement('div');
  d.textContent=x==null?'':String(x);return d.innerHTML;}
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
  $('#app').innerHTML=`
  <div class="card"><h2>Shop</h2>
    <div class="row"><div class="name"><label>Shop name</label>
      <input id="shop-title" value="${esc(sh.title)}"></div>
      <div><button class="sub" id="shop-save">Save</button></div></div>
  </div>
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
  const rs=$('#restock'),pl=$('#plist');
  const openRestock=()=>{rs.style.display='';pl.style.display='none';
    $('#restock-on').style.display='none';
    rs.scrollIntoView({behavior:'smooth',block:'start'});};
  $('#restock-on').onclick=openRestock;
  if(MODE==='restock')openRestock();
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
  $('#shop-save').onclick=async()=>{
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
