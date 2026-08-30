"""Quote-and-suggest order routing: which vendor can fill a site order cheapest?

When a paid website order arrives at /notify, this module quotes vendor shops
(all bot shops except the SPBC master shop) against the order's lines:

- a vendor qualifies only if they can fill EVERY line from current stock
- vial lines cost qty × price; kit lines use kit_price when set (stock must
  cover qty × KIT_SIZE vials), else KIT_SIZE × vial price

SPBC SMS-sourced peptides are fulfilled by Remy from Show Me Source stock —
not passed to Unicorn (Ghostie's shop). Unicorn stays online for her own
customers; we just skip her (and any shop flagged for skip) when quoting
springfieldpbc.com orders. If she was the only complete-fill vendor, the
owner gets no vendor quotes and fulfills himself.

The owner gets a DM listing the top quotes (with margin) and an approve button.

Approving sends the vendor an OFFER, not an order:

    quoted → offered → accepted | declined | expired

Nothing moves until the vendor taps Accept: no stock is deducted and the
customer's shipping address is NOT included in the offer. On Accept the stock
is re-checked, deducted with an 'order_route' audit trail, and the address is
released. On Decline/expiry the owner is told and can offer the next-cheapest
vendor with one tap. The owner can also override and push immediately.

The regular supplier notify flow runs regardless of any of this.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from typing import Any, Optional

import db
from config import KIT_SIZE, SPBC_SHOP_CHAT_ID

log = logging.getLogger("order_router")

# Title fragments that identify Unicorn / Ghostie's customer shop. Shop
# chat_id is not hardcoded in this repo (UNICORN_SHOP_CHAT_ID is optional).
_SKIP_TITLE_MARKERS = ("unicorn", "ghostie", "unicornmagicfactory")


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _parse_int_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip().strip("\"'")
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


def skip_ids_for_spbc_fulfillment() -> set[int]:
    """Explicit shop ids never quoted for SPBC website fulfillment."""
    ids = _parse_int_ids(os.getenv("SKIP_VENDOR_SHOP_CHAT_IDS", ""))
    ids |= _parse_int_ids(os.getenv("UNICORN_SHOP_CHAT_ID", ""))
    return ids


def skip_shop_for_spbc_fulfillment(
    shop_chat_id: int, shop_title: str = ""
) -> bool:
    """True if this vendor must not be quoted for SPBC website orders.

    Identifies Unicorn by title (unicorn / ghostie / unicornmagicfactory)
    and by SKIP_VENDOR_SHOP_CHAT_IDS / UNICORN_SHOP_CHAT_ID. Patriotic
    Peptides and other vendors are unchanged. Unicorn's catalog and stock
    are not modified — we only refuse to offer her SPBC SMS peptide lines.
    """
    try:
        cid = int(shop_chat_id)
    except (TypeError, ValueError):
        return False
    if cid in skip_ids_for_spbc_fulfillment():
        return True
    # SKIP_UNICORN_ROUTING defaults on: title match is how we find her
    # when UNICORN_SHOP_CHAT_ID is unset.
    if not _truthy_env("SKIP_UNICORN_ROUTING", "1"):
        return False
    title = str(shop_title or "").lower()
    return any(marker in title for marker in _SKIP_TITLE_MARKERS)


QUOTE_TTL_SEC = 48 * 60 * 60
MAX_SUGGESTIONS = 3

# How long a vendor has to answer an offer before the owner is nudged
try:
    OFFER_TTL_SEC = max(5, int(os.getenv("VENDOR_OFFER_TTL_MIN", "360"))) * 60
except ValueError:
    OFFER_TTL_SEC = 6 * 60 * 60

# Quote lifecycle states
QUOTED = "quoted"
OFFERED = "offered"
ACCEPTED = "accepted"
DECLINED = "declined"
EXPIRED = "expired"

# quote_id -> {order_number, shop_chat_id, shop_title, total, order_total,
#              lines, shipping, created_at, applied, state, offered_at,
#              decline_reason}
_pending: dict[str, dict] = {}
# order_number -> {shop_chat_id, shop_title, total, at} for orders a vendor
# accepted. Kept after the quotes are cleared so a shipping address that
# arrives later can still be forwarded to whoever is fulfilling.
_routed: dict[str, dict] = {}
_lock = threading.Lock()


def routed_vendor(order_number: str) -> Optional[dict]:
    """Which vendor is fulfilling this order, if any."""
    with _lock:
        r = _routed.get(str(order_number or ""))
        return dict(r) if r else None


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
    shop = db.get_shop(int(shop_chat_id)) or {}
    # SPBC SMS peptides are fulfilled by Remy, not passed to Unicorn.
    if skip_shop_for_spbc_fulfillment(
        int(shop_chat_id), str(shop.get("title") or "")
    ):
        return None
    products = db.list_products(int(shop_chat_id), active_only=True)
    by_name = {_norm(p["name"]): p for p in products}
    total = 0.0
    breakdown: list[dict] = []
    for line in lines:
        # An explicit admin mapping wins: a vendor calling SPBC's "HGH 360IU"
        # their "H36" can still be quoted. Falls back to matching by name.
        p = None
        try:
            import vendor_links

            p = vendor_links.product_for(line["base"], int(shop_chat_id))
        except Exception as exc:
            log.warning("vendor link lookup failed shop=%s: %s", shop_chat_id, exc)
        if p is None:
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
        # SPBC SMS peptides are fulfilled by Remy, not passed to Unicorn.
        if skip_shop_for_spbc_fulfillment(cid, str(shop.get("title") or "")):
            continue
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
    try:
        order_total = float(payload.get("total_cents") or 0) / 100.0
    except (TypeError, ValueError):
        order_total = 0.0
    out = []
    with _lock:
        for q in quotes[:MAX_SUGGESTIONS]:
            qid = secrets.token_hex(6)
            _pending[qid] = {
                "order_number": order_number,
                "shop_chat_id": q["shop_chat_id"],
                "shop_title": q["shop_title"],
                "total": q["total"],
                "order_total": order_total,
                "lines": q["breakdown"],
                "shipping": shipping,
                "created_at": time.time(),
                "applied": False,
                "state": QUOTED,
                "offered_at": None,
                "decline_reason": "",
            }
            # return the stored record so callers see order_total/state too
            out.append((qid, dict(_pending[qid])))
    return out


# ── Offer lifecycle ─────────────────────────────────────────────────────────

def offer_quote(quote_id: str) -> tuple[bool, str, Optional[dict]]:
    """quoted → offered. No stock moves; address stays withheld."""
    with _lock:
        q = _pending.get(quote_id)
        if q is None:
            return False, "Quote expired or unknown.", None
        if q["state"] == ACCEPTED:
            return False, "Already accepted.", dict(q)
        if q["state"] == OFFERED:
            return False, "Already waiting on this vendor.", dict(q)
        q["state"] = OFFERED
        q["offered_at"] = time.time()
        return True, "Offer sent.", dict(q)


def decline_quote(
    quote_id: str, actor_id: int, reason: str = ""
) -> tuple[bool, str, Optional[dict]]:
    with _lock:
        q = _pending.get(quote_id)
        if q is None:
            return False, "Offer expired or unknown.", None
        if q["state"] == ACCEPTED:
            return False, "Already accepted — can't decline now.", dict(q)
        if q["state"] == DECLINED:
            return False, "Already declined.", dict(q)
        q["state"] = DECLINED
        q["decline_reason"] = str(reason or "")[:200]
        return True, "Declined.", dict(q)


def expire_offer(quote_id: str) -> Optional[dict]:
    """Mark an unanswered offer expired. Returns the quote if it was pending."""
    with _lock:
        q = _pending.get(quote_id)
        if q is None or q["state"] != OFFERED:
            return None
        q["state"] = EXPIRED
        return dict(q)


def alternatives_for(order_number: str, exclude_quote_id: str = "") -> list[tuple[str, dict]]:
    """Other vendors still available for this order, cheapest first."""
    _prune()
    with _lock:
        items = [
            (qid, dict(q))
            for qid, q in _pending.items()
            if q["order_number"] == order_number
            and qid != exclude_quote_id
            and q["state"] == QUOTED
        ]
    items.sort(key=lambda kv: kv[1]["total"])
    return items


def can_accept(quote_id: str, user_id: int) -> tuple[bool, str, Optional[dict]]:
    """Only an admin of the offered vendor shop may accept/decline."""
    q = get_quote(quote_id)
    if q is None:
        return False, "Offer expired or unknown.", None
    if not db.is_admin(int(q["shop_chat_id"]), int(user_id)):
        return False, "Only this shop's admins can answer.", None
    return True, "", q


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
    order_total = 0.0
    for _, q in registered:
        order_total = float(q.get("order_total") or 0) or order_total
    lines = [f"💡 Vendor quotes for order {order_number}"]
    if order_total:
        lines.append(f"Customer paid: ${order_total:.2f}")
    lines += ["These vendors can fill the whole order from stock:", ""]
    buttons = []
    for i, (qid, q) in enumerate(registered, 1):
        tag = " ← cheapest" if i == 1 else ""
        cost = float(q["total"])
        line = f"{i}. {q['shop_title']} — cost ${cost:.2f}{tag}"
        if order_total:
            margin = order_total - cost
            pct = (margin / order_total * 100) if order_total else 0
            line += f"\n    margin ${margin:.2f} ({pct:.0f}%)"
        lines.append(line)
        buttons.append(
            [
                {
                    "text": f"📨 Offer to {q['shop_title'][:22]} (${cost:.2f})",
                    "callback_data": f"routeq:{qid}",
                }
            ]
        )
    lines += [
        "",
        "Offering asks the vendor to Accept first — no stock moves and the "
        "customer's address stays private until they do. Your regular "
        "suppliers were notified as usual.",
    ]
    buttons.append([{"text": "✖️ Dismiss", "callback_data": f"routeq_x:{order_number}"}])
    return {"text": "\n".join(lines), "reply_markup": {"inline_keyboard": buttons}}


def build_vendor_offer(quote: dict) -> str:
    """Offer WITHOUT the customer address — released only after Accept."""
    lines = [
        "🤝 Fulfillment offer from SPBC",
        f"Order: {quote['order_number']}",
        "",
        "Can you fill this from your stock?",
        "",
    ]
    for ln in quote["lines"]:
        kind = " (kit)" if ln["kind"] == "kit" else ""
        lines.append(f"• {ln['qty']}× {ln['name']}{kind} — ${ln['cost']:.2f}")
    lines += [
        f"\nYou'd be paid: ${quote['total']:.2f}",
        "",
        "Tap Accept and we'll send the shipping address and reduce your stock. "
        "Decline and nothing happens — we'll ask someone else.",
    ]
    return "\n".join(lines)


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

    # Record the payable: this is the only moment we know what we owe them.
    try:
        import payables

        payables.record(quote)
    except Exception as exc:
        log.error("payable not recorded for %s: %s", quote["order_number"], exc)

    with _lock:
        if quote_id in _pending:
            _pending[quote_id]["state"] = ACCEPTED
        _routed[str(quote["order_number"])] = {
            "shop_chat_id": quote["shop_chat_id"],
            "shop_title": quote["shop_title"],
            "total": quote["total"],
            "at": time.time(),
        }
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
