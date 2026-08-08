"""Quote-and-suggest order routing: which vendor can fill a site order cheapest?

When a paid website order arrives at /notify, this module quotes every vendor
shop (all bot shops except the SPBC master shop) against the order's lines:

- a vendor qualifies only if they can fill EVERY line from current stock
- vial lines cost qty × price; kit lines use kit_price when set (stock must
  cover qty × KIT_SIZE vials), else KIT_SIZE × vial price

The owner gets a DM listing the top quotes with approve buttons. Approving
sends that vendor a fulfillment request (their own prices, so totals are fine
to show) and deducts their stock with an 'order_route' audit trail. Nothing is
automatic: no button tap → nothing changes, and the regular supplier notify
flow runs regardless.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any, Optional

import db
from config import KIT_SIZE, SPBC_SHOP_CHAT_ID

log = logging.getLogger("order_router")

QUOTE_TTL_SEC = 48 * 60 * 60
MAX_SUGGESTIONS = 3

# quote_id -> {order_number, shop_chat_id, shop_title, total, lines, shipping,
#              created_at, applied}
_pending: dict[str, dict] = {}
_lock = threading.Lock()


def _prune() -> None:
    now = time.time()
    with _lock:
        stale = [
            k for k, q in _pending.items() if now - q["created_at"] > QUOTE_TTL_SEC
        ]
        for k in stale:
            _pending.pop(k, None)


def _norm(name: str) -> str:
    return " ".join(str(name).split()).lower()


_KIT_MARKERS = ("(kit)", "(10-pack)", "(10-pack / kit)", "(pack)")


def parse_line(item: dict) -> Optional[dict]:
    """Order item → {base, qty, kind} where kind is 'vial' or 'kit'."""
    name = str(item.get("name") or item.get("sku") or "").strip()
    if not name:
        return None
    try:
        qty = int(item.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return None
    low = name.lower()
    kind = "vial"
    base = name
    for marker in _KIT_MARKERS:
        if low.endswith(marker):
            kind = "kit"
            base = name[: len(name) - len(marker)].strip()
            break
    else:
        if low.endswith("(vial)"):
            base = name[: -len("(vial)")].strip()
    return {"base": base, "qty": qty, "kind": kind}


def quote_shop(shop_chat_id: int, lines: list[dict]) -> Optional[dict]:
    """Total + per-line breakdown if this shop can fill every line, else None."""
    products = db.list_products(int(shop_chat_id), active_only=True)
    by_name = {_norm(p["name"]): p for p in products}
    total = 0.0
    breakdown: list[dict] = []
    for line in lines:
        p = by_name.get(_norm(line["base"]))
        if p is None:
            return None
        stock = int(p.get("stock") or 0)
        price = float(p.get("price") or 0)
        if price <= 0:
            return None
        if line["kind"] == "kit":
            vials_needed = line["qty"] * KIT_SIZE
            if stock < vials_needed:
                return None
            kit_price = p.get("kit_price")
            unit_cost = float(kit_price) if kit_price else price * KIT_SIZE
            cost = unit_cost * line["qty"]
            deduct = vials_needed
        else:
            if stock < line["qty"]:
                return None
            cost = price * line["qty"]
            deduct = line["qty"]
        total += cost
        breakdown.append(
            {
                "product_id": int(p["id"]),
                "name": str(p["name"]),
                "qty": line["qty"],
                "kind": line["kind"],
                "deduct": deduct,
                "cost": cost,
            }
        )
    return {"total": round(total, 2), "breakdown": breakdown}


def compute_quotes(payload: dict) -> list[dict]:
    """All complete-fill vendor quotes for an order payload, cheapest first."""
    items = payload.get("items")
    items = items if isinstance(items, list) else []
    lines = [ln for ln in (parse_line(it) for it in items if isinstance(it, dict)) if ln]
    if not lines:
        return []
    with db.get_db() as conn:
        rows = conn.execute("SELECT chat_id, title FROM shops").fetchall()
        shops = [dict(r) for r in rows]
    quotes = []
    for shop in shops:
        cid = int(shop["chat_id"])
        if SPBC_SHOP_CHAT_ID and cid == int(SPBC_SHOP_CHAT_ID):
            continue  # the master shop is the site itself, not a vendor
        try:
            q = quote_shop(cid, lines)
        except Exception as exc:
            log.warning("quote failed shop=%s: %s", cid, exc)
            continue
        if q is not None:
            quotes.append(
                {"shop_chat_id": cid, "shop_title": str(shop["title"]), **q}
            )
    quotes.sort(key=lambda q: q["total"])
    return quotes


def register_quotes(payload: dict, quotes: list[dict]) -> list[tuple[str, dict]]:
    """Store top quotes for approval buttons. Returns [(quote_id, quote)]."""
    _prune()
    order_number = str(
        payload.get("order_number") or payload.get("orderNumber") or "UNKNOWN"
    )
    shipping = payload.get("shipping") if isinstance(payload.get("shipping"), dict) else None
    out = []
    with _lock:
        for q in quotes[:MAX_SUGGESTIONS]:
            qid = secrets.token_hex(6)
            _pending[qid] = {
                "order_number": order_number,
                "shop_chat_id": q["shop_chat_id"],
                "shop_title": q["shop_title"],
                "total": q["total"],
                "lines": q["breakdown"],
                "shipping": shipping,
                "created_at": time.time(),
                "applied": False,
            }
            out.append((qid, q))
    return out


def get_quote(quote_id: str) -> Optional[dict]:
    _prune()
    with _lock:
        q = _pending.get(quote_id)
        return dict(q) if q else None


def dismiss_order(order_number: str) -> int:
    with _lock:
        stale = [
            k for k, q in _pending.items() if q["order_number"] == order_number
        ]
        for k in stale:
            _pending.pop(k, None)
        return len(stale)


def build_owner_suggestion(order_number: str, registered: list[tuple[str, dict]]) -> dict:
    """Text + inline keyboard spec (Telegram sendMessage reply_markup dict)."""
    lines = [
        f"💡 Vendor quotes for order {order_number}",
        "These vendors can fill the whole order from current bot stock:",
        "",
    ]
    buttons = []
    for i, (qid, q) in enumerate(registered, 1):
        tag = " ← cheapest" if i == 1 else ""
        lines.append(f"{i}. {q['shop_title']} — ${q['total']:.2f}{tag}")
        buttons.append(
            [
                {
                    "text": f"✅ Route to {q['shop_title'][:24]} (${q['total']:.2f})",
                    "callback_data": f"routeq:{qid}",
                }
            ]
        )
    lines += [
        "",
        "Approving sends the vendor a fulfillment request and deducts "
        "their bot stock. Doing nothing changes nothing — your regular "
        "suppliers were notified as usual.",
    ]
    buttons.append([{"text": "✖️ Dismiss", "callback_data": f"routeq_x:{order_number}"}])
    return {"text": "\n".join(lines), "reply_markup": {"inline_keyboard": buttons}}


def build_vendor_message(quote: dict) -> str:
    lines = [
        "📦 Fulfillment request from SPBC",
        f"Order: {quote['order_number']}",
        "",
        "Items (at your listed prices):",
    ]
    for ln in quote["lines"]:
        kind = " (kit)" if ln["kind"] == "kit" else ""
        lines.append(f"• {ln['qty']}× {ln['name']}{kind} — ${ln['cost']:.2f}")
    lines.append(f"\nTotal owed to you: ${quote['total']:.2f}")
    ship = quote.get("shipping")
    if ship and (ship.get("line1") or ship.get("name")):
        lines += ["", "Ship to:"]
        city = ", ".join(
            str(v) for v in (ship.get("city"), ship.get("state"), ship.get("postal")) if v
        )
        for s in (ship.get("name"), ship.get("line1"), ship.get("line2"), city,
                  ship.get("country"), ship.get("phone")):
            if s:
                lines.append(str(s))
    lines += [
        "",
        "Your stock was already reduced for these items. Questions → message "
        "the SPBC owner.",
    ]
    return "\n".join(lines)


def apply_route(quote_id: str, actor_id: int) -> tuple[bool, str, Optional[dict]]:
    """Deduct the vendor's stock for an approved quote (idempotent).

    Returns (ok, message, quote). Stock is re-checked at apply time — a sale
    between quote and approval fails the route instead of going negative.
    """
    with _lock:
        quote = _pending.get(quote_id)
        if quote is None:
            return False, "Quote expired or unknown.", None
        if quote["applied"]:
            return False, "Already routed.", dict(quote)
        quote["applied"] = True  # claim before slow work; revert on failure

    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    try:
        with db.get_db() as conn:
            # Verify all stock first so a failed line doesn't half-deduct
            for ln in quote["lines"]:
                row = conn.execute(
                    "SELECT stock FROM products WHERE id = ? AND chat_id = ?",
                    (ln["product_id"], quote["shop_chat_id"]),
                ).fetchone()
                if row is None or int(row["stock"]) < ln["deduct"]:
                    raise ValueError(
                        f"Stock changed: {ln['name']} now short "
                        f"({0 if row is None else row['stock']} left, "
                        f"need {ln['deduct']})."
                    )
            for ln in quote["lines"]:
                row = conn.execute(
                    "SELECT stock FROM products WHERE id = ?", (ln["product_id"],)
                ).fetchone()
                before = int(row["stock"])
                after = before - ln["deduct"]
                conn.execute(
                    "UPDATE products SET stock = ?, updated_at = ? WHERE id = ?",
                    (after, now, ln["product_id"]),
                )
                conn.execute(
                    "INSERT INTO stock_audit (chat_id, product_id, product_name, "
                    "delta, stock_before, stock_after, reason, actor_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'order_route', ?, ?)",
                    (
                        quote["shop_chat_id"],
                        ln["product_id"],
                        ln["name"],
                        -ln["deduct"],
                        before,
                        after,
                        int(actor_id),
                        now,
                    ),
                )
    except ValueError as exc:
        with _lock:
            if quote_id in _pending:
                _pending[quote_id]["applied"] = False
        return False, str(exc), None
    except Exception as exc:
        with _lock:
            if quote_id in _pending:
                _pending[quote_id]["applied"] = False
        log.error("apply_route failed: %s", exc, exc_info=exc)
        return False, "Routing failed — vendor stock unchanged.", None

    log.info(
        "order_route applied order=%s shop=%s total=%s",
        quote["order_number"],
        quote["shop_chat_id"],
        quote["total"],
    )
    return True, "Routed.", dict(quote)


def suggest_for_order(payload: dict) -> Optional[dict]:
    """Compute + register quotes; returns owner message spec or None."""
    quotes = compute_quotes(payload)
    if not quotes:
        return None
    registered = register_quotes(payload, quotes)
    order_number = str(
        payload.get("order_number") or payload.get("orderNumber") or "UNKNOWN"
    )
    return build_owner_suggestion(order_number, registered)
