"""Safe catalog cleanup for Unicorn-style unique-name imports.

The unique-name importer jammed `(vial)` / `(kit)` and `$15.00` into product
names so add-only import would not skip them. That left:

- `Anav@r 25mg` typos
- `Aod 5mg (vial) $15.00` next to `Aod 5mg (vial) $130.00` (vial vs kit)
- two real products that happen to share a cleaned name (do NOT merge those)

Safety:
- Never DELETE / DROP / wipe a shop.
- Deactivate loser rows so order history keeps its product_id.
- Do not change keeper stock.
- Owner-gated apply; dry-run by default.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

import db

# Kit-of-10 with a typical discount is ~7–10× vial. True different SKUs
# (B12 $10 vs $15, Snap 8 $8 vs $50 / 6.25×) sit below this floor.
# Upper bound rejects wild outliers. 7.0 keeps Aod $15/$130 (8.67×).
MIN_KIT_RATIO = 7.0
MAX_KIT_RATIO = 12.0

DEFAULT_UNICORN_TITLE = "Unicorn Magic Factory"
_GENERIC_SHOP_TITLES = frozenset(
    {
        "shop",
        "store",
        "new vendor",
        "new shop",
        "unicornfartzzbot",
        "unicornfartzz",
    }
)

_PRICE_TAIL = re.compile(
    r"""
    (?:\s*\((?:vial|kit)s?\))?   # uniqueness suffix from fix_unicorn_names
    \s*\$\s*\d+(?:\.\d+)?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_UNIT_SUFFIX = re.compile(r"\s*\((?:vial|kit)s?\)\s*$", re.IGNORECASE)
_ANAVAR_TYPO = re.compile(r"\banav@r\b", re.IGNORECASE)
_MD_UNSAFE = re.compile(r"([_*`\[\]])")
# Telegram InlineKeyboardButton.text is 1–64 characters. Over-long labels
# are rejected or sliced mid-glyph (� / boxes) on the catalog.
TG_BUTTON_MAX = 64


ActionKind = Literal["rename", "merge", "deactivate"]


@dataclass
class CleanupAction:
    kind: ActionKind
    product_id: int
    name: str
    detail: str
    keeper_id: int | None = None
    new_name: str | None = None
    kit_price: float | None = None


@dataclass
class CleanupPlan:
    shop_chat_id: int
    actions: list[CleanupAction] = field(default_factory=list)
    groups: int = 0

    @property
    def rename_count(self) -> int:
        return sum(1 for a in self.actions if a.kind == "rename")

    @property
    def merge_count(self) -> int:
        return sum(1 for a in self.actions if a.kind == "merge")

    @property
    def deactivate_count(self) -> int:
        return sum(1 for a in self.actions if a.kind == "deactivate")


# Classic mojibake markers: UTF-8 bytes shown after a cp1252/latin-1 mis-decode.
# Emoji become ð.. / â.. runs; accented Latin becomes Ã. / Â. pairs.
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "ð", "Å", "Ÿ", "€", "š", "œ", "ž")


def repair_glyphs(text: str) -> str:
    """Best-effort repair of UTF-8 text mis-decoded as cp1252/latin-1 (mojibake).

    Conservative on purpose: only touches strings that still carry classic
    mojibake markers, and only accepts a re-decode that is clean UTF-8 (no
    U+FFFD). Text already stored correctly (real emoji, plain ASCII, accented
    Latin) is returned unchanged — real emoji cannot round-trip through a
    single-byte codec, so the encode step raises and we bail. Loops a few
    times to undo double-encoding.
    """
    s = str(text or "")
    for _ in range(3):
        if not any(m in s for m in _MOJIBAKE_MARKERS):
            break
        fixed: str | None = None
        for codec in ("cp1252", "latin-1"):
            try:
                cand = s.encode(codec, "strict").decode("utf-8", "strict")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if "�" in cand or cand == s:
                continue
            fixed = cand
            break
        if fixed is None:
            break
        s = fixed
    return s


def display_shop_text(text: str) -> str:
    """Repair mojibake in shop title / welcome while preserving line breaks."""
    s = repair_glyphs(str(text or ""))
    return s.replace("�", "").replace("­", "")


def buyer_shop_title(title: str | None, *, unicorn: bool = False) -> str:
    """Buyer-facing shop title. Generic Unicorn placeholders become the real name."""
    s = display_shop_text(str(title or "")).strip()
    s = " ".join(s.split())
    if unicorn and (not s or s.casefold() in _GENERIC_SHOP_TITLES):
        return DEFAULT_UNICORN_TITLE
    return s


def maybe_persist_unicorn_title(shop: dict | None) -> str | None:
    """If the Unicorn catalog shop is still named Shop, persist the real title.

    Shop-scoped UPDATE only. Never touches products or /data wipe paths.
    """
    if not shop:
        return None
    try:
        sid = int(shop.get("chat_id") or 0)
    except (TypeError, ValueError):
        return None
    if not sid:
        return None
    current = str(shop.get("title") or "")
    wanted = buyer_shop_title(current, unicorn=True)
    if not wanted or wanted == " ".join(current.split()):
        return None
    db.update_shop(sid, title=wanted)
    return wanted


def sanitize_catalog_text(text: str) -> str:
    """Drop control/format/replacement glyphs that render as boxes or �."""
    s = repair_glyphs(str(text or ""))
    if not s:
        return ""
    s = s.replace("\ufffd", "").replace("\u00ad", "")
    try:
        s = unicodedata.normalize("NFKC", s)
    except Exception:
        pass
    out: list[str] = []
    for ch in s:
        if unicodedata.category(ch)[0] == "C":
            continue
        out.append(ch)
    return " ".join("".join(out).split())


def md_escape(text: str) -> str:
    """Escape Telegram legacy Markdown metacharacters in product strings."""
    s = str(text or "")
    return (
        s.replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


def clean_product_name(name: str) -> str:
    """Strip uniqueness-hack tails, typos, and invisible junk. Does not invent titles."""
    n = sanitize_catalog_text(name)
    if not n:
        return ""
    n = _ANAVAR_TYPO.sub("Anavar", n)
    n = _PRICE_TAIL.sub("", n).strip()
    n = _UNIT_SUFFIX.sub("", n).strip()
    n = re.sub(r"\s{2,}", " ", n).strip(" -–—")
    return n[:120] if n else ""


def display_product_name(name: str) -> str:
    """Buyer-facing name: cleaned uniqueness tails, never empty."""
    return clean_product_name(name) or sanitize_catalog_text(name) or "Item"


def catalog_button_label(
    name: str,
    price: float | None = None,
    stock: int | None = None,
    *,
    guest: str | None = None,
    max_len: int = TG_BUTTON_MAX,
) -> str:
    """Catalog row label that always fits Telegram's 64-character button cap."""
    shown = display_product_name(name)
    try:
        price_bit = f"${float(price):.2f}" if price is not None else ""
    except (TypeError, ValueError):
        price_bit = ""
    try:
        stock_i: int | None = int(stock) if stock is not None else None
    except (TypeError, ValueError):
        stock_i = None
    guest_s = sanitize_catalog_text(guest or "")
    guest_bit = f" (+{guest_s})" if guest_s else ""

    def _tail(include_stock: bool) -> str:
        bits: list[str] = []
        if price_bit:
            bits.append(price_bit)
        if include_stock and stock_i is not None:
            bits.append("(out)" if stock_i <= 0 else f"{stock_i} left")
        core = " · ".join(bits)
        if not core:
            return guest_bit
        return f" · {core}{guest_bit}"

    max_len = max(8, int(max_len))
    tail = _tail(True)
    if len(shown) + len(tail) > max_len:
        tail = _tail(False)
    if len(shown) + len(tail) > max_len:
        budget = max_len - len(tail) - 1
        if budget < 4:
            tail = guest_bit
            budget = max_len - len(tail) - 1
        if budget < 1:
            return (shown + tail)[:max_len]
        shown = shown[:budget].rstrip(" ·-/,") + "…"
    return (shown + tail)[:max_len]


def grouping_key(name: str) -> str:
    return clean_product_name(name).casefold()


def should_merge_prices(lo: float, hi: float) -> bool:
    """True when hi looks like a kit-of-10 price for lo (not a sibling SKU)."""
    try:
        lo_f = float(lo)
        hi_f = float(hi)
    except (TypeError, ValueError):
        return False
    if lo_f <= 0 or hi_f <= lo_f:
        return False
    ratio = hi_f / lo_f
    return MIN_KIT_RATIO <= ratio <= MAX_KIT_RATIO


def _desc_key(p: dict) -> str:
    return " ".join(str(p.get("description") or "").split()).casefold()


def descriptions_block_ratio_merge(keeper: dict, other: dict) -> bool:
    """Different (or one-sided) descriptions mean distinct SKUs, not a kit pair."""
    a = _desc_key(keeper)
    b = _desc_key(other)
    if not a and not b:
        return False
    return a != b


def _disambiguate_name(cleaned: str, p: dict) -> str:
    """Keep sibling SKUs distinguishable when they share a cleaned name."""
    base = cleaned or display_product_name(str(p.get("name") or ""))
    desc = " ".join(str(p.get("description") or "").split())
    snippet = desc[:28].strip(" ,;/-") if desc else ""
    if snippet and snippet.casefold() not in base.casefold():
        return f"{base} ({snippet})"[:120]
    price = _price(p)
    if price > 0:
        return f"{base} (${price:.2f})"[:120]
    return base[:120]


def _unit(p: dict) -> str:
    """Prefer the stored unit; (kit) jammed into the name still counts as kit."""
    u = (str(p.get("unit") or "vial")).strip().lower() or "vial"
    name = str(p.get("name") or "")
    if re.search(r"\(kits?\)", name, re.I):
        return "kit"
    if re.search(r"\(vials?\)", name, re.I):
        return "vial"
    return u


def _price(p: dict) -> float:
    try:
        return float(p.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _same_price(a: float, b: float) -> bool:
    return round(float(a), 2) == round(float(b), 2)


def _esc(s: str) -> str:
    return _MD_UNSAFE.sub(r"\\\1", s or "")


def _pick_keeper(group: list[dict]) -> dict:
    vials = [p for p in group if _unit(p) == "vial"]
    pool = vials or group
    return min(pool, key=lambda p: (_price(p), int(p.get("id") or 0)))


def _needs_rename(p: dict, cleaned: str) -> bool:
    current = " ".join(str(p.get("name") or "").split())
    return bool(cleaned) and current != cleaned


def plan_cleanup(chat_id: int, products: list[dict] | None = None) -> CleanupPlan:
    """Build a shop-scoped plan. Inactive rows are left alone."""
    plan = CleanupPlan(shop_chat_id=int(chat_id))
    if products is None:
        products = db.list_products(int(chat_id), active_only=True)
    buckets: dict[str, list[dict]] = {}
    for p in products:
        if int(p.get("active") or 0) != 1:
            continue
        key = grouping_key(str(p.get("name") or ""))
        if not key:
            continue
        buckets.setdefault(key, []).append(p)

    for key, group in buckets.items():
        cleaned = clean_product_name(str(group[0].get("name") or ""))
        if len(group) == 1:
            p = group[0]
            if _needs_rename(p, cleaned):
                plan.actions.append(
                    CleanupAction(
                        kind="rename",
                        product_id=int(p["id"]),
                        name=str(p["name"]),
                        detail="strip uniqueness tail / typo",
                        new_name=cleaned,
                    )
                )
            continue

        plan.groups += 1
        keeper = _pick_keeper(group)
        keeper_id = int(keeper["id"])
        keeper_price = _price(keeper)
        kit_price: float | None = None
        merge_ids: set[int] = set()
        dup_ids: set[int] = set()
        stay: list[dict] = []

        existing_kit = keeper.get("kit_price")
        try:
            if existing_kit and float(existing_kit) > 0:
                kit_price = float(existing_kit)
        except (TypeError, ValueError):
            pass

        for p in group:
            pid = int(p["id"])
            if pid == keeper_id:
                continue
            unit = _unit(p)
            price = _price(p)
            # Original row + uniqueness-hack copy at the same vial price.
            if _same_price(price, keeper_price):
                dup_ids.add(pid)
                continue
            ratio_ok = should_merge_prices(keeper_price, price) and not (
                descriptions_block_ratio_merge(keeper, p)
            )
            if unit == "kit" or ratio_ok:
                if price > keeper_price:
                    kit_price = max(kit_price or 0.0, price)
                    merge_ids.add(pid)
                else:
                    dup_ids.add(pid)
            else:
                stay.append(p)

        if merge_ids and kit_price and kit_price > keeper_price:
            plan.actions.append(
                CleanupAction(
                    kind="merge",
                    product_id=keeper_id,
                    name=str(keeper["name"]),
                    detail=(
                        f"vial ${keeper_price:.2f} + kit ${kit_price:.2f}"
                    ),
                    keeper_id=keeper_id,
                    kit_price=kit_price,
                )
            )
            for p in group:
                pid = int(p["id"])
                if pid in merge_ids:
                    plan.actions.append(
                        CleanupAction(
                            kind="deactivate",
                            product_id=pid,
                            name=str(p["name"]),
                            detail=f"merged into #{keeper_id}",
                            keeper_id=keeper_id,
                        )
                    )
        else:
            # Kit merge did not apply — those rows stay as siblings.
            stay.extend(p for p in group if int(p["id"]) in merge_ids)
            merge_ids.clear()

        keeper_name = cleaned
        if stay:
            keeper_name = _disambiguate_name(cleaned, keeper)
        if _needs_rename(keeper, keeper_name):
            plan.actions.append(
                CleanupAction(
                    kind="rename",
                    product_id=keeper_id,
                    name=str(keeper["name"]),
                    detail=(
                        "sibling SKU — not a kit pair"
                        if stay
                        else "strip uniqueness tail / typo"
                    ),
                    new_name=keeper_name,
                )
            )

        for p in group:
            pid = int(p["id"])
            if pid in dup_ids:
                plan.actions.append(
                    CleanupAction(
                        kind="deactivate",
                        product_id=pid,
                        name=str(p["name"]),
                        detail=f"duplicate of #{keeper_id} (same price)",
                        keeper_id=keeper_id,
                    )
                )

        for p in stay:
            other_clean = clean_product_name(str(p.get("name") or ""))
            new_name = other_clean
            if new_name and new_name.casefold() == cleaned.casefold():
                new_name = _disambiguate_name(cleaned, p)
            if _needs_rename(p, new_name):
                plan.actions.append(
                    CleanupAction(
                        kind="rename",
                        product_id=int(p["id"]),
                        name=str(p["name"]),
                        detail="sibling SKU — not a kit pair",
                        new_name=new_name,
                    )
                )
    return plan


def apply_cleanup(
    chat_id: int,
    *,
    actor_id: int,
    dry_run: bool = True,
    plan: CleanupPlan | None = None,
    owner_required: bool = True,
) -> tuple[bool, str, CleanupPlan]:
    """Apply a plan. Owner check is the caller's job (bot / tests)."""
    if owner_required and not db.is_owner(int(actor_id)):
        return False, "Bot owner only.", plan or CleanupPlan(shop_chat_id=int(chat_id))
    plan = plan or plan_cleanup(int(chat_id))
    if dry_run:
        return True, format_preview(plan, dry_run=True), plan
    if not plan.actions:
        return True, "Nothing to clean in this shop.", plan

    now = db._utc_now()
    applied = 0
    with db.get_db() as conn:
        for action in plan.actions:
            row = conn.execute(
                "SELECT * FROM products WHERE id = ? AND chat_id = ?",
                (int(action.product_id), int(chat_id)),
            ).fetchone()
            if not row:
                continue
            if action.kind == "rename" and action.new_name:
                conn.execute(
                    "UPDATE products SET name = ?, updated_at = ? "
                    "WHERE id = ? AND chat_id = ?",
                    (action.new_name, now, int(action.product_id), int(chat_id)),
                )
                applied += 1
            elif action.kind == "merge":
                fields = ["updated_at = ?"]
                vals: list[Any] = [now]
                if action.new_name:
                    fields.append("name = ?")
                    vals.append(action.new_name)
                if action.kit_price and action.kit_price > 0:
                    fields.append("kit_price = ?")
                    vals.append(float(action.kit_price))
                if _unit(dict(row)) != "vial":
                    fields.append("unit = ?")
                    vals.append("vial")
                vals.extend([int(action.product_id), int(chat_id)])
                conn.execute(
                    f"UPDATE products SET {', '.join(fields)} "
                    "WHERE id = ? AND chat_id = ?",
                    vals,
                )
                applied += 1
            elif action.kind == "deactivate":
                conn.execute(
                    "UPDATE products SET active = 0, updated_at = ? "
                    "WHERE id = ? AND chat_id = ?",
                    (now, int(action.product_id), int(chat_id)),
                )
                conn.execute(
                    "INSERT INTO stock_audit (chat_id, product_id, product_name, "
                    "delta, stock_before, stock_after, reason, actor_id, created_at) "
                    "VALUES (?, ?, ?, 0, ?, ?, 'catalog_cleanup_deactivate', ?, ?)",
                    (
                        int(chat_id),
                        int(action.product_id),
                        str(row["name"]),
                        int(row["stock"] or 0),
                        int(row["stock"] or 0),
                        int(actor_id),
                        now,
                    ),
                )
                # Copy photo / COA onto keeper if the keeper is missing them.
                if action.keeper_id:
                    keeper = conn.execute(
                        "SELECT * FROM products WHERE id = ? AND chat_id = ?",
                        (int(action.keeper_id), int(chat_id)),
                    ).fetchone()
                    if keeper:
                        copies: list[str] = []
                        cvals: list[Any] = []
                        for col in (
                            "photo_file_id",
                            "coa_url",
                            "coa_file_id",
                            "coa_file_type",
                            "coa_filename",
                            "category",
                            "sku",
                        ):
                            if col not in row.keys() or col not in keeper.keys():
                                continue
                            have = (keeper[col] or "") if keeper[col] is not None else ""
                            src = (row[col] or "") if row[col] is not None else ""
                            if str(src).strip() and not str(have).strip():
                                copies.append(f"{col} = ?")
                                cvals.append(row[col])
                        if copies:
                            cvals.extend([now, int(action.keeper_id), int(chat_id)])
                            conn.execute(
                                "UPDATE products SET "
                                + ", ".join(copies)
                                + ", updated_at = ? WHERE id = ? AND chat_id = ?",
                                cvals,
                            )
                applied += 1
    msg = (
        f"Catalog cleanup applied on this shop: "
        f"{plan.rename_count} rename(s), {plan.merge_count} merge(s), "
        f"{plan.deactivate_count} deactivated (history kept). "
        f"Touched {applied} row(s). No products deleted."
    )
    return True, msg, plan


def open_order_product_refs(chat_id: int, product_ids: list[int]) -> list[dict]:
    """Open (not cancelled/rejected/complete) order lines for these product ids."""
    ids = [int(x) for x in product_ids if x]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with db.get_db() as conn:
        if "order_items" not in {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            return []
        rows = conn.execute(
            f"""
            SELECT oi.product_id AS product_id, o.id AS order_id, o.status AS status
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE o.chat_id = ?
              AND oi.product_id IN ({placeholders})
              AND o.status NOT IN ('cancelled', 'rejected', 'complete', 'shipped')
            """,
            (int(chat_id), *ids),
        ).fetchall()
    return [dict(r) for r in rows]


def apply_bound_shop_cleanup(shop_chat_id: int, *, actor_id: int | None = None) -> dict:
    """Owner-gated apply for a bound vendor shop (boot / live). Never deletes."""
    from config import OWNER_IDS as owners

    sid = int(shop_chat_id)
    actor = int(actor_id) if actor_id is not None else (min(owners) if owners else 0)
    require_owner = bool(owners)
    if require_owner and not actor:
        return {"ok": False, "skipped": "no OWNER_IDS"}
    plan = plan_cleanup(sid)
    hide_ids = [a.product_id for a in plan.actions if a.kind == "deactivate"]
    open_refs = open_order_product_refs(sid, hide_ids)
    ok, msg, plan = apply_cleanup(
        sid,
        actor_id=actor or 0,
        dry_run=False,
        plan=plan,
        owner_required=require_owner,
    )
    return {
        "ok": ok,
        "msg": msg,
        "renames": plan.rename_count,
        "merges": plan.merge_count,
        "deactivated": plan.deactivate_count,
        "open_order_refs": len(open_refs),
        "shop_chat_id": sid,
    }


def format_preview(plan: CleanupPlan, *, dry_run: bool = True, max_lines: int = 12) -> str:
    header = "Catalog cleanup preview" if dry_run else "Catalog cleanup"
    lines = [
        f"🧹 *{_esc(header)}*",
        "",
        f"Renames: *{plan.rename_count}*",
        f"Vial+kit merges: *{plan.merge_count}*",
        f"Deactivate \\(keep order history\\): *{plan.deactivate_count}*",
        "",
    ]
    if not plan.actions:
        lines.append("_Nothing to clean in this shop._")
        return "\n".join(lines)

    shown = 0
    for a in plan.actions:
        if shown >= max_lines:
            left = len(plan.actions) - shown
            lines.append(f"_…and {left} more._")
            break
        if a.kind == "rename":
            lines.append(
                f"• {_esc(a.name)} → *{_esc(a.new_name or '')}*"
            )
        elif a.kind == "merge":
            extra = f" → {_esc(a.new_name)}" if a.new_name else ""
            lines.append(f"• merge #{a.product_id} {extra} — {_esc(a.detail)}")
        elif a.kind == "deactivate":
            lines.append(
                f"• hide #{a.product_id} {_esc(a.name)} \\({_esc(a.detail)}\\)"
            )
        shown += 1
    lines.append("")
    lines.append("Does *not* delete products. Stock on the keeper is unchanged.")
    return "\n".join(lines)
