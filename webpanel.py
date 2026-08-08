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

import hashlib
import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import db
import inventory_import

log = logging.getLogger("webpanel")

TOKEN_TTL_HOURS = 72
INVITE_TTL_HOURS = 14 * 24
MAX_BULK_BYTES = 100_000


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
            """
        )


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


def panel_url(base_url: str, raw_token: str) -> str:
    return f"{base_url.rstrip('/')}/panel?t={urllib.parse.quote(raw_token)}"


# ── Vendor invites ───────────────────────────────────────────────────────────

def create_vendor_invite(created_by: int, note: str = "") -> str:
    ensure_webpanel_tables()
    # Telegram start payloads allow [A-Za-z0-9_-] up to 64 chars, but '_'/'-'
    # break Markdown parsing when the link is rendered — keep it hex.
    raw = secrets.token_hex(12)
    now = _utc_now()
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO vendor_invites (token_hash, note, created_by, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?)",
            (
                _hash(raw),
                note.strip()[:80],
                int(created_by),
                _ts(now),
                _ts(now + timedelta(hours=INVITE_TTL_HOURS)),
            ),
        )
    return raw


def redeem_vendor_invite(raw: str, user_id: int) -> tuple[bool, str]:
    """One-time redemption. Returns (ok, message)."""
    ensure_webpanel_tables()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM vendor_invites WHERE token_hash = ?", (_hash(raw),)
        ).fetchone()
        if not row:
            return False, "Invite not found."
        if row["used_by"] is not None:
            return False, "Invite already used."
        if _ts(_utc_now()) > str(row["expires_at"]):
            return False, "Invite expired — ask for a fresh link."
        conn.execute(
            "UPDATE vendor_invites SET used_by = ?, used_at = ? WHERE id = ?",
            (int(user_id), _ts(_utc_now()), int(row["id"])),
        )
    return True, str(row["note"] or "")


# ── JSON API (pure functions; HTTP layer is a thin wrapper) ─────────────────

def _err(code: int, msg: str) -> tuple[int, dict]:
    return code, {"ok": False, "error": msg}


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
    return {
        "id": p["id"],
        "name": p["name"],
        "price": p["price"],
        "kit_price": p.get("kit_price"),
        "stock": p.get("stock", 0),
        "unit": p.get("unit") or "vial",
        "active": int(p.get("active") or 0),
        "site_key": p.get("site_key"),
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

    name = str(payload.get("name") or "").strip()[:60]
    instructions = str(payload.get("instructions") or "").strip()[:1000]

    if mid is None:
        if not name:
            return _err(400, "Name required")
        new_id = db.add_payment_method(chat_id, name, instructions)
        return 200, {"ok": True, "id": new_id, "created": True}

    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name
    if payload.get("instructions") is not None:
        fields["instructions"] = instructions
    if payload.get("active") is not None:
        fields["active"] = 1 if payload["active"] in (1, True, "1", "true") else 0
    if not fields:
        return _err(400, "Nothing to update")
    db.update_payment_method(mid, **fields)
    return 200, {"ok": True, "id": mid, "created": False}


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
</style>
</head>
<body>
<h1 id="title">Shop Panel</h1>
<div id="app"><div class="card">Loading… If this never loads, your link
expired — send <b>/webpanel</b> to the bot for a fresh one.</div></div>
<div class="msg" id="msg"></div>
<script>
const T=new URLSearchParams(location.search).get('t')||'';
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
    <div id="plist">${S.products.map(prodRow).join('')}</div>
    <div class="prod"><div class="row">
      <div class="name"><label>New product</label><input id="np-name" placeholder="BPC-157 10MG"></div>
      <div class="num"><label>Price</label><input id="np-price" type="number" step="0.01" min="0"></div>
      <div class="num"><label>Stock</label><input id="np-stock" type="number" step="1" min="0" value="0"></div>
      <div><button id="np-add">Add</button></div></div></div>
    <div class="prod"><label>Bulk add / update — one per line:
      name | price | stock</label>
      <textarea id="bulk" placeholder="BPC-157 10MG | 41 | 10&#10;TB-500 10MG | 47 | 5"></textarea>
      <div class="flex" style="margin-top:8px">
        <span class="tag grow">Existing names update; new names are created.</span>
        <button class="sub" id="bulk-go">Import</button></div></div>
  </div>
  <div class="card"><h2>Payment methods</h2>
    <div id="paylist">${S.payments.map(m=>`
      <div class="prod${m.active?'':' off'}" data-mid="${m.id}">
        <div class="row"><div class="name"><label>Name</label>
          <input class="p-name" value="${esc(m.name)}"></div></div>
        <label>Instructions shown to buyers</label>
        <textarea class="p-instr" style="min-height:60px">${esc(m.instructions)}</textarea>
        <div class="flex" style="margin-top:8px">
          <label class="flex" style="margin:0"><input type="checkbox" class="p-act"
            style="width:auto" ${m.active?'checked':''}> enabled</label>
          <span class="grow"></span>
          <button class="danger p-del">Delete</button>
          <button class="sub p-save">Save</button></div></div>`).join('')}
    <div class="prod"><div class="row">
      <div class="name"><label>New method (Cash App, Zelle, crypto…)</label>
        <input id="pm-name" placeholder="Cash App"></div>
      <div><button id="pm-add">Add</button></div></div></div>
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
function wire(){
  $('#shop-save').onclick=async()=>{
    const d=await api('shop',{title:$('#shop-title').value});
    if(d.ok){toast('Shop name saved');S.shop.title=$('#shop-title').value;
      $('#title').textContent=S.shop.title;}};
  document.querySelectorAll('#plist .prod').forEach(el=>{
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
      price:$('#np-price').value,stock:$('#np-stock').value||0});
    if(d.ok){toast('Product added');load();}};
  $('#bulk-go').onclick=async()=>{
    const d=await api('bulk',{text:$('#bulk').value});
    if(d.ok){toast(`Imported: ${d.created} new, ${d.updated} updated`+
      (d.errors.length?`, ${d.errors.length} errors`:''));load();}};
  document.querySelectorAll('#paylist .prod').forEach(el=>{
    el.querySelector('.p-save').onclick=async()=>{
      const d=await api('payment',{id:el.dataset.mid,
        name:el.querySelector('.p-name').value,
        instructions:el.querySelector('.p-instr').value,
        active:el.querySelector('.p-act').checked});
      if(d.ok)toast('Saved');};
    el.querySelector('.p-del').onclick=async()=>{
      if(!confirm('Delete this payment method?'))return;
      const d=await api('payment',{id:el.dataset.mid,delete:true});
      if(d.ok){toast('Deleted');load();}};});
  $('#pm-add').onclick=async()=>{
    const d=await api('payment',{name:$('#pm-name').value,
      instructions:''});
    if(d.ok){toast('Added — now write its instructions');load();}};
  $('#sh-save').onclick=async()=>{
    const d=await api('shipping',{enabled:$('#sh-on').checked,
      fee:$('#sh-fee').value,free_above:$('#sh-free').value});
    if(d.ok)toast('Shipping saved');};}
load();
</script>
</body>
</html>
"""
