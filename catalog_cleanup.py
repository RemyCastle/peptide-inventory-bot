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
from urllib.parse import unquote_to_bytes

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
# Telegram InlineKeyboardButton.text is capped at 64 UTF-16 code units.
# Python len() under-counts non-BMP emoji (🦄 is 1 char / 2 units), so a
# naive s[:64] either overflows Telegram or slices a combining/ZWJ run.
TG_BUTTON_MAX = 64
# Format chars we must keep so emoji ZWJ sequences and subdivision flags
# (🏴 + TAG latin + CANCEL TAG, e.g. 🏴󠁧󠁢󠁥󠁮󠁧󠁿) survive the sanitizer.
_ZWJ = "\u200d"
_CANCEL_TAG = "\U000e007f"
_TAG_MIN, _TAG_MAX = 0xE0020, 0xE007F
_KEEP_CF = frozenset((_ZWJ,)) | frozenset(
    chr(c) for c in range(_TAG_MIN, _TAG_MAX + 1)
)
_CRLF_PCT = re.compile(r"%0[0da]|%00", re.IGNORECASE)
# Line-breaking C0 / DEL / Zl-Zp in the raw URL, plus C0/DEL percent-encoded.
# NUL is stripped by storefront_label (dirty catalog) and is not a break.
_UNSAFE_URL_RAW = re.compile(
    r"[\x09\x0a\x0b\x0c\x0d\x7f\u2028\u2029]|%(?:0[0-9a-fA-F]|1[0-9a-fA-F]|7[fF])"
)
# Invisible "blank" glyphs that are not Cf/Cc (so they survived prior passes).
# U+115F/U+1160 are the NFKC forms of Hangul fillers — drop those too.
# U+034F / Mongolian FVS are Mn (so they survived _drop_controls' C-class skip).
_INVISIBLE_FILLERS = frozenset(
    {
        "\uFFFC",  # object replacement
        "\u2800",  # braille blank
        "\u3164",  # hangul filler
        "\uFFA0",  # halfwidth hangul filler (NFKC → U+3164 → U+1160)
        "\u115F",  # hangul choseong filler
        "\u1160",  # hangul jungseong filler
        "\u034F",  # combining grapheme joiner
        "\u180B",  # mongolian free variation selector 1
        "\u180C",  # mongolian fvs 2
        "\u180D",  # mongolian fvs 3
    }
)


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


def _is_regional_indicator(ch: str) -> bool:
    o = ord(ch)
    return 0x1F1E6 <= o <= 0x1F1FF


def _is_emoji_tag(ch: str) -> bool:
    o = ord(ch)
    return _TAG_MIN <= o <= _TAG_MAX


def _is_fitzpatrick(ch: str) -> bool:
    return 0x1F3FB <= ord(ch) <= 0x1F3FF


def _is_variation_selector(ch: str) -> bool:
    o = ord(ch)
    return o in (0xFE0E, 0xFE0F) or 0xE0100 <= o <= 0xE01EF


def _is_noncharacter(ch: str) -> bool:
    o = ord(ch)
    if 0xFDD0 <= o <= 0xFDEF:
        return True
    return (o & 0xFFFE) == 0xFFFE


def _drop_controls(text: str, *, keep_newlines: bool = False) -> str:
    """Drop Cc/Cf/Cs/Co/nonchars except ZWJ/emoji-tags (and newlines).

    Zl/Zp (U+2028/U+2029) become a newline when keep_newlines else a space
    so they cannot glue tokens or hide a URL break.
    """
    out: list[str] = []
    for ch in text:
        if keep_newlines and ch in "\r\n":
            out.append(ch)
            continue
        if _is_noncharacter(ch) or ch in _INVISIBLE_FILLERS:
            continue
        cat = unicodedata.category(ch)
        if cat in ("Zl", "Zp"):
            out.append("\n" if keep_newlines else " ")
            continue
        if cat == "Co":
            continue
        if cat[0] == "C" and ch not in _KEEP_CF:
            continue
        out.append(ch)
    return "".join(out)


def _strip_leading_orphans(text: str) -> str:
    """Drop combining marks / VS / skin-tone / tags with no base glyph."""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        cat = unicodedata.category(ch)
        if (
            cat in ("Mn", "Me")
            or _is_fitzpatrick(ch)
            or _is_variation_selector(ch)
            or _is_emoji_tag(ch)
        ):
            i += 1
            continue
        break
    return text[i:]


def _strip_incomplete_tags(text: str) -> str:
    """Keep only complete flag-tag sequences (🏴 + TAG latin + CANCEL TAG).

    Orphan tags (no black flag), a lone CANCEL TAG, and black-flag + cancel
    with no region letters are dropped. Pirate 🏴‍☠️ is black-flag + ZWJ, not
    tags — the flag stays and the ZWJ run is handled elsewhere.
    """
    if not text or not any(_is_emoji_tag(ch) for ch in text):
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    black_flag = "\U0001F3F4"
    while i < n:
        ch = text[i]
        if ch == black_flag:
            j = i + 1
            while j < n and _is_emoji_tag(text[j]):
                j += 1
            tags = text[i + 1 : j]
            if len(tags) >= 2 and tags[-1] == _CANCEL_TAG:
                out.append(text[i:j])
            else:
                out.append(black_flag)
            i = j
            continue
        if _is_emoji_tag(ch):
            i += 1
            while i < n and _is_emoji_tag(text[i]):
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _maybe_mojibake_char(ch: str) -> bool:
    """True for latin-1 / cp1252 code points that can carry UTF-8 mojibake."""
    if ord(ch) < 256:
        return True
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _repair_loop(s: str) -> str:
    """Undo cp1252/latin-1 mojibake on a run that can round-trip those codecs."""
    if not s:
        return s
    for _ in range(3):
        if not any(m in s for m in _MOJIBAKE_MARKERS):
            break
        fixed: str | None = None
        for codec in ("cp1252", "latin-1"):
            try:
                cand = s.encode(codec, "strict").decode("utf-8", "strict")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if "\ufffd" in cand or cand == s:
                continue
            fixed = cand
            break
        if fixed is None:
            break
        s = fixed
    return s


def repair_glyphs(text: str) -> str:
    """Best-effort repair of UTF-8 text mis-decoded as cp1252/latin-1 (mojibake).

    Conservative on purpose: only touches runs that still carry classic
    mojibake markers, and only accepts a re-decode that is clean UTF-8 (no
    U+FFFD). Already-correct emoji cannot round-trip a single-byte codec, so
    a mixed string (real 🧬 next to leftover ðŸ) is split: the latin run is
    repaired and the real emoji is kept. Loops a few times to undo
    double-encoding.
    """
    # BOM / replacement must go first or they block a cp1252 re-encode.
    s = str(text or "").replace("\ufeff", "").replace("\ufffd", "")
    whole = _repair_loop(s)
    if whole != s or not any(m in s for m in _MOJIBAKE_MARKERS):
        return whole.replace("\ufffd", "")
    out: list[str] = []
    buf: list[str] = []
    for ch in s:
        if _maybe_mojibake_char(ch):
            buf.append(ch)
            continue
        if buf:
            out.append(_repair_loop("".join(buf)))
            buf.clear()
        out.append(ch)
    if buf:
        out.append(_repair_loop("".join(buf)))
    return "".join(out).replace("\ufffd", "")


def display_shop_text(text: str) -> str:
    """Repair mojibake in shop title / welcome while preserving line breaks."""
    s = repair_glyphs(str(text or "")).replace("\u00ad", "")
    s = _drop_controls(s, keep_newlines=True)
    s = _strip_leading_orphans(s)
    s = _strip_incomplete_tags(s)
    return s.replace("\r\n", "\n").replace("\r", "\n")


def buyer_shop_title(title: str | None, *, unicorn: bool = False) -> str:
    """Buyer-facing shop title. Generic Unicorn placeholders become the real name."""
    s = sanitize_catalog_text(display_shop_text(str(title or "")))
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


def utf16_len(text: str) -> int:
    """UTF-16 code units — the unit Telegram uses for the 64-char button cap."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in str(text or ""))


def clip_label(text: str, max_len: int, *, ellipsis: str = "…") -> str:
    """Trim to max_len UTF-16 units without splitting a code point or ZWJ run."""
    s = _strip_incomplete_tags(_strip_leading_orphans(_drop_controls(str(text or ""))))
    max_len = int(max_len)
    if max_len <= 0:
        return ""
    if utf16_len(s) <= max_len:
        return s
    ell = ellipsis if ellipsis and utf16_len(ellipsis) < max_len else ""
    budget = max_len - utf16_len(ell)
    out: list[str] = []
    used = 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > budget:
            break
        out.append(ch)
        used += w
    # ZWJ joins to the *next* code point — a trailing one is always dangling.
    # Do not pop VS-16 / combining marks: those bind to the previous character
    # and are complete if we already kept the base (☂️, café).
    # Do not pop a complete flag-tag sequence (ends in CANCEL TAG).
    while out and out[-1] == _ZWJ:
        used -= 2 if ord(out[-1]) > 0xFFFF else 1
        out.pop()
    if out and _is_emoji_tag(out[-1]) and out[-1] != _CANCEL_TAG:
        while out and _is_emoji_tag(out[-1]):
            used -= 2 if ord(out[-1]) > 0xFFFF else 1
            out.pop()
    if out and _is_regional_indicator(out[-1]):
        run = 0
        for ch in reversed(out):
            if _is_regional_indicator(ch):
                run += 1
            else:
                break
        if run % 2 == 1:
            out.pop()
    clipped = "".join(out).rstrip(" ·-/,")
    return clipped + ell


def sanitize_catalog_text(text: str) -> str:
    """Drop control/format/replacement glyphs that render as boxes or �.

    Keeps ZWJ (U+200D) and emoji tag chars (U+E0020–U+E007F) so ZWJ sequences
    and subdivision flags stay intact. Other Cf (ZWSP, BOM, soft hyphen) still
    go. Whitespace collapses; line breaks are not preserved — use
    display_shop_text for title/welcome.
    """
    s = repair_glyphs(str(text or ""))
    if not s:
        return ""
    s = s.replace("\u00ad", "")
    try:
        s = unicodedata.normalize("NFKC", s)
    except Exception:
        pass
    s = _drop_controls(s)
    s = _strip_leading_orphans(s)
    s = _strip_incomplete_tags(s)
    return " ".join(s.split())


def storefront_label(text: str | None, max_len: int | None = None) -> str:
    """Buyer-facing catalog field (sku, category, variant, payment name)."""
    s = sanitize_catalog_text(str(text or ""))
    if max_len is not None and int(max_len) > 0:
        s = clip_label(s, int(max_len), ellipsis="")
    return s


def _percent_payload_unsafe(text: str) -> bool:
    """True when percent-decoding yields C0/Cf/bidi/line-sep/nonchars/invalid UTF-8.

    Loops a few times so `%2500` (double-encoded NUL) cannot sneak through.
    `%20` spaces and emoji code points stay allowed.

    URLs are not product names: ZWJ / emoji tags (`_KEEP_CF`) that we keep
    in catalog labels still fail closed here, so `exam%E2%80%8Dple.com`
    cannot spoof `example.com`. NBSP / other Zs besides SPACE also fail
    (`%C2%A0`); combining marks (Mn/Me) too.
    """
    s = str(text or "")
    if not s:
        return False
    for _ in range(3):
        try:
            raw_b = unquote_to_bytes(s)
        except Exception:
            return True
        if any(b < 0x20 or b == 0x7F for b in raw_b):
            return True
        try:
            decoded = raw_b.decode("utf-8")
        except UnicodeDecodeError:
            return True
        for ch in decoded:
            if _is_noncharacter(ch) or ch in _INVISIBLE_FILLERS:
                return True
            cat = unicodedata.category(ch)
            if cat in ("Zl", "Zp", "Cs", "Co", "Mn", "Me"):
                return True
            if cat == "Zs" and ch != " ":
                return True
            if cat[0] == "C":
                return True
        if decoded == s:
            return False
        s = decoded
    return False


def public_http_url(value: str | None, max_len: int = 500) -> str:
    """Buyer-facing http(s) URL. Glyph junk stripped; non-http dropped.

    Rejects raw C0 / line-sep / percent-encoded C0 so stripping controls
    cannot glue `https://x.com/a\\nSet-Cookie` into a still-http URL.
    Userinfo (`user:pass@host`) is also refused. Percent-decoded bidi /
    line-sep / overlong UTF-8 (`%E2%80%AE`, `%C0%80`, `%2500`) fail closed.
    ZWJ / emoji tags / NBSP in the host or path (`%E2%80%8D`, `%C2%A0`)
    also fail closed — those stay allowed only on catalog labels.
    """
    raw = str(value or "")
    if _UNSAFE_URL_RAW.search(raw):
        return ""
    s = storefront_label(value, max_len)
    if (
        not s
        or _CRLF_PCT.search(s)
        or _UNSAFE_URL_RAW.search(s)
        or _percent_payload_unsafe(s)
    ):
        return ""
    low = s.lower()
    if low.startswith("https://"):
        rest, scheme = s[8:], "https://"
    elif low.startswith("http://"):
        rest, scheme = s[7:], "http://"
    else:
        return ""
    if not rest or rest.startswith("/") or " " in rest or "\\" in rest:
        return ""
    host_part = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not host_part or "@" in host_part:
        return ""
    if host_part.startswith("["):
        end = host_part.find("]")
        if end < 2:
            return ""
    else:
        hostname = host_part.split(":", 1)[0]
        labels = hostname.rstrip(".").split(".")
        if not hostname or not labels or any(not lab for lab in labels):
            return ""
    return scheme + rest


def sanitize_multiline(text: str | None, max_len: int | None = None) -> str:
    """Repair glyphs; keep newlines; drop other controls. Welcome / instructions."""
    s = display_shop_text(str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    if not s:
        return ""
    out: list[str] = []
    for ch in s:
        if ch == "\n":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat[0] == "C" and ch not in _KEEP_CF:
            continue
        out.append(ch)
    lines = [" ".join(part.split()) for part in "".join(out).split("\n")]
    s = "\n".join(lines).strip()
    if max_len is not None and int(max_len) > 0 and utf16_len(s) > int(max_len):
        s = clip_label(s, int(max_len), ellipsis="")
    return s


def public_shipping_zones(zones: list[dict] | None) -> list[dict] | None:
    """Buyer-facing zone ids/labels: repaired, junk stripped. Empty → None."""
    if not zones:
        return None
    out: list[dict] = []
    for z in zones:
        zid = storefront_label(z.get("id"), 40)
        if not zid:
            continue
        label = storefront_label(z.get("label"), 80) or zid
        item = dict(z)
        item["id"] = zid
        item["label"] = label
        out.append(item)
    return out or None


def tg_button_text(text: str, max_len: int = TG_BUTTON_MAX) -> str:
    """Shop-picker / menu label: repair glyphs, collapse newlines, UTF-16 cap."""
    s = sanitize_catalog_text(display_shop_text(str(text or "")).replace("\n", " "))
    if not s:
        return ""
    return clip_label(s, max(1, int(max_len)))


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
    return clip_label(n, 120, ellipsis="") if n else ""


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
    if utf16_len(shown) + utf16_len(tail) > max_len:
        tail = _tail(False)
    if utf16_len(shown) + utf16_len(tail) > max_len:
        ell_w = utf16_len("…")
        budget = max_len - utf16_len(tail) - ell_w
        if budget < 4:
            tail = guest_bit
            budget = max_len - utf16_len(tail) - ell_w
        if budget < 1:
            return clip_label(shown + tail, max_len, ellipsis="")
        shown = clip_label(shown.rstrip(" ·-/,"), budget + ell_w)
    result = shown + tail
    if utf16_len(result) > max_len:
        return clip_label(result, max_len, ellipsis="")
    return result


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
