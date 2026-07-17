"""Parse layout text files and create shop products (add-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import db

# Safety caps for Telegram-uploaded files
MAX_FILE_BYTES = 200_000
MAX_PRODUCT_LINES = 200
MAX_NAME_LEN = 120
MAX_DESC_LEN = 500

TEMPLATE_TEXT = """# UnicornFartzz inventory import
# One product per line. Format:
#   name | price | stock | description (optional)
# Lines starting with # are ignored. Blank lines ignored.
#
# Examples:
Tren Ace | 45.00 | 10 | acetate blend
Test E | 30 | 5
HCG 5000 | 55.00 | 0 | fridge item
"""


@dataclass
class ParsedRow:
    name: str
    price: float
    stock: int
    description: str = ""
    line_no: int = 0


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _norm_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


def parse_inventory_text(text: str) -> ParseResult:
    """
    Parse pipe-separated inventory layout.

    Required: name | price | stock
    Optional: | description
    """
    result = ParseResult()
    if text is None:
        result.errors.append("Empty file.")
        return result

    # Strip UTF-8 BOM
    if text.startswith("\ufeff"):
        text = text[1:]

    product_lines = 0
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            result.errors.append(
                f"L{line_no}: need at least name | price | stock (got {len(parts)} field(s))"
            )
            continue

        name, price_s, stock_s = parts[0], parts[1], parts[2]
        description = " | ".join(parts[3:]).strip() if len(parts) > 3 else ""

        # Header row
        if name.casefold() == "name" and price_s.casefold() in ("price", "cost"):
            continue

        if not name:
            result.errors.append(f"L{line_no}: name is empty")
            continue
        if len(name) > MAX_NAME_LEN:
            result.errors.append(f"L{line_no}: name too long (max {MAX_NAME_LEN})")
            continue
        if len(description) > MAX_DESC_LEN:
            description = description[:MAX_DESC_LEN]

        price_s = price_s.replace("$", "").replace(",", "").strip()
        try:
            price = float(price_s)
        except ValueError:
            result.errors.append(f"L{line_no}: bad price \"{parts[1]}\"")
            continue
        if price <= 0:
            result.errors.append(f"L{line_no}: price must be > 0")
            continue

        try:
            stock = int(float(stock_s.replace(",", "").strip()))
        except ValueError:
            result.errors.append(f"L{line_no}: bad stock \"{parts[2]}\"")
            continue
        if stock < 0:
            result.errors.append(f"L{line_no}: stock cannot be negative")
            continue

        product_lines += 1
        if product_lines > MAX_PRODUCT_LINES:
            result.errors.append(
                f"L{line_no}: too many products (max {MAX_PRODUCT_LINES} per file)"
            )
            break

        result.rows.append(
            ParsedRow(
                name=name,
                price=price,
                stock=stock,
                description=description,
                line_no=line_no,
            )
        )

    if not result.rows and not result.errors:
        result.errors.append("No product lines found in file.")

    return result


def import_products(
    chat_id: int,
    rows: list[ParsedRow],
    *,
    skip_existing: bool = True,
) -> ImportResult:
    """
    Create products for a shop (add-only).

    When skip_existing is True (default), names that already exist in this shop
    (case-insensitive) are skipped without changing price/stock.
    """
    out = ImportResult()
    if not rows:
        return out

    existing = {
        _norm_name(p["name"]): p
        for p in db.list_products(int(chat_id), active_only=False)
    }
    # Also track names created in this batch so duplicate lines in one file skip
    batch_seen: set[str] = set()

    for row in rows:
        key = _norm_name(row.name)
        if not key:
            out.errors.append(f"L{row.line_no}: empty name after normalize")
            continue
        if key in batch_seen:
            out.skipped.append(row.name)
            continue
        if skip_existing and key in existing:
            out.skipped.append(row.name)
            continue
        try:
            pid = db.add_product(
                int(chat_id),
                name=row.name,
                price=row.price,
                stock=row.stock,
                description=row.description or "",
            )
            out.created.append(row.name)
            batch_seen.add(key)
            existing[key] = {"id": pid, "name": row.name}
        except Exception as exc:  # noqa: BLE001 — report per-row, keep going
            out.errors.append(f"L{row.line_no}: failed to add {row.name}: {exc}")

    return out


def import_from_text(chat_id: int, text: str) -> tuple[ParseResult, ImportResult]:
    """Parse then import; returns both result objects."""
    parsed = parse_inventory_text(text)
    imported = import_products(int(chat_id), parsed.rows, skip_existing=True)
    # Fold parse errors into import summary visibility (caller can use both)
    return parsed, imported


def decode_upload_bytes(data: bytes) -> str:
    """Decode uploaded file bytes as UTF-8 (BOM ok); fall back to latin-1."""
    if not data:
        return ""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"File too large (max {MAX_FILE_BYTES // 1000} KB).")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def format_import_summary(
    parsed: ParseResult, imported: ImportResult, *, max_list: int = 8
) -> str:
    """Human-readable Telegram reply (Markdown-safe enough with plain names)."""
    lines = [
        "📥 *Inventory import*",
        f"Created: *{imported.created_count}*",
        f"Skipped (already exist): *{imported.skipped_count}*",
        f"Parse/line errors: *{len(parsed.errors) + len(imported.errors)}*",
    ]
    if imported.created:
        sample = ", ".join(imported.created[:max_list])
        extra = len(imported.created) - max_list
        lines.append(f"\n✅ Added: {sample}" + (f" (+{extra} more)" if extra > 0 else ""))
    if imported.skipped:
        sample = ", ".join(imported.skipped[:max_list])
        extra = len(imported.skipped) - max_list
        lines.append(
            f"\n⏭ Skipped: {sample}" + (f" (+{extra} more)" if extra > 0 else "")
        )
    all_errs = list(parsed.errors) + list(imported.errors)
    if all_errs:
        lines.append("\n⚠️ Errors:")
        for e in all_errs[:12]:
            lines.append(f"• {e}")
        if len(all_errs) > 12:
            lines.append(f"• …and {len(all_errs) - 12} more")
    if imported.created_count == 0 and imported.skipped_count == 0 and all_errs:
        lines.insert(1, "_Nothing was created._")
    return "\n".join(lines)
