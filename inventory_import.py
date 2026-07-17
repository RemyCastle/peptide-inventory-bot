"""Parse layout text files and create/update shop products (import + mass edit)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import db

# Safety caps for Telegram-uploaded files
MAX_FILE_BYTES = 200_000
MAX_PRODUCT_LINES = 200
MAX_NAME_LEN = 120
MAX_DESC_LEN = 500
MAX_UNIT_LEN = 20
DEFAULT_UNIT = "vial"

ImportMode = Literal["add_only", "update_only", "upsert"]

TEMPLATE_TEXT = """# UnicornFartzz inventory import / mass edit
# One product per line. Preferred format:
#   name | price | stock | unit | description
# Lines starting with # are ignored. Blank lines ignored.
#
# Units: vial, bottle, pack, kit, ea, etc. (default: vial)
#
# Examples:
Tren Ace | 45.00 | 10 | vial | acetate blend
Test E | 30 | 5 | bottle |
HCG 5000 | 55.00 | 0 | kit | fridge item
#
# Legacy (still works): name | price | stock | description
# (4 fields after name/price/stock treated as description, unit = vial)
"""


@dataclass
class ParsedRow:
    name: str
    price: float
    stock: int
    unit: str = DEFAULT_UNIT
    description: str = ""
    line_no: int = 0
    # True when file explicitly included a unit column (5+ fields)
    unit_explicit: bool = False


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def updated_count(self) -> int:
        return len(self.updated)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _norm_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


def normalize_unit(unit: str | None, *, default: str = DEFAULT_UNIT) -> str:
    """Sanitize product unit label (vial, bottle, pack, …)."""
    u = " ".join(str(unit or "").strip().split())
    if not u or u == "-":
        return default
    # No pipes/newlines in unit (file delimiters)
    u = u.replace("|", "/").replace("\n", " ").replace("\r", " ")
    if len(u) > MAX_UNIT_LEN:
        u = u[:MAX_UNIT_LEN]
    return u or default


def parse_inventory_text(text: str) -> ParseResult:
    """
    Parse pipe-separated inventory layout.

    Preferred: name | price | stock | unit | description
    Legacy:    name | price | stock
               name | price | stock | description
    """
    result = ParseResult()
    if text is None:
        result.errors.append("Empty file.")
        return result

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
        unit = DEFAULT_UNIT
        description = ""
        unit_explicit = False

        if len(parts) == 3:
            pass
        elif len(parts) == 4:
            # Legacy: 4th field is description (no unit column)
            description = parts[3]
        else:
            # 5+: name | price | stock | unit | description…
            unit_explicit = True
            unit = normalize_unit(parts[3])
            description = " | ".join(parts[4:]).strip()

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
                unit=unit,
                description=description,
                line_no=line_no,
                unit_explicit=unit_explicit,
            )
        )

    if not result.rows and not result.errors:
        result.errors.append("No product lines found in file.")

    return result


def import_products(
    chat_id: int,
    rows: list[ParsedRow],
    *,
    mode: ImportMode = "add_only",
    skip_existing: bool | None = None,
) -> ImportResult:
    """
    Apply product rows to a shop.

    Modes:
      add_only    — create new names only; skip existing (legacy default)
      update_only — update existing by name; skip unknown
      upsert      — update if exists, else create

    skip_existing=True forces add_only for backward compatibility.
    """
    if skip_existing is True:
        mode = "add_only"
    elif skip_existing is False and mode == "add_only":
        # Historical: skip_existing=False meant create even if exists → treat as upsert
        mode = "upsert"

    out = ImportResult()
    if not rows:
        return out

    existing = {
        _norm_name(p["name"]): p
        for p in db.list_products(int(chat_id), active_only=False)
    }
    batch_seen: set[str] = set()

    for row in rows:
        key = _norm_name(row.name)
        if not key:
            out.errors.append(f"L{row.line_no}: empty name after normalize")
            continue
        if key in batch_seen:
            out.skipped.append(row.name)
            continue

        unit = normalize_unit(row.unit)
        found = existing.get(key)

        try:
            if found is None:
                if mode == "update_only":
                    out.skipped.append(row.name)
                    batch_seen.add(key)
                    continue
                pid = db.add_product(
                    int(chat_id),
                    name=row.name,
                    price=row.price,
                    stock=row.stock,
                    description=row.description or "",
                    unit=unit,
                )
                out.created.append(row.name)
                batch_seen.add(key)
                existing[key] = {
                    "id": pid,
                    "name": row.name,
                    "price": row.price,
                    "stock": row.stock,
                    "unit": unit,
                    "description": row.description or "",
                }
                continue

            # Exists
            if mode == "add_only":
                out.skipped.append(row.name)
                batch_seen.add(key)
                continue

            pid = int(found["id"])
            # On update: always set price/stock/description; unit if explicit or always
            fields: dict[str, Any] = {
                "price": float(row.price),
                "stock": int(row.stock),
                "description": row.description or "",
                "unit": unit,
            }
            ok = db.update_product(pid, **fields)
            if not ok:
                out.errors.append(f"L{row.line_no}: failed to update {row.name}")
                continue
            out.updated.append(row.name)
            batch_seen.add(key)
            existing[key] = {
                **found,
                **fields,
                "id": pid,
                "name": found.get("name") or row.name,
            }
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"L{row.line_no}: failed {row.name}: {exc}")

    return out


def import_from_text(
    chat_id: int,
    text: str,
    *,
    mode: ImportMode = "add_only",
) -> tuple[ParseResult, ImportResult]:
    """Parse then apply with the given mode."""
    parsed = parse_inventory_text(text)
    imported = import_products(int(chat_id), parsed.rows, mode=mode)
    return parsed, imported


def export_inventory_text(
    chat_id: int,
    *,
    active_only: bool = False,
    shop_title: str | None = None,
) -> str:
    """
    Export shop catalog as mass-edit .txt (5-column format).
    """
    products = db.list_products(int(chat_id), active_only=active_only)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = (shop_title or "shop").strip() or "shop"
    lines = [
        f"# Inventory export — {title}",
        f"# Exported {day} UTC · shop_id={chat_id}",
        "# Edit and re-upload via Admin → Mass edit",
        "# Format: name | price | stock | unit | description",
        "# Matching is by product name (case-insensitive).",
        "",
        "name | price | stock | unit | description",
    ]
    for p in products:
        name = str(p.get("name") or "").replace("|", "/")
        desc = str(p.get("description") or "").replace("\n", " ").replace("|", "/")
        unit = normalize_unit(p.get("unit"))
        price = float(p.get("price") or 0)
        stock = int(p.get("stock") or 0)
        lines.append(f"{name} | {price:.2f} | {stock} | {unit} | {desc}".rstrip())
    lines.append("")
    return "\n".join(lines)


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
    parsed: ParseResult,
    imported: ImportResult,
    *,
    mode: ImportMode = "add_only",
    max_list: int = 8,
) -> str:
    """Human-readable Telegram reply."""
    mode_label = {
        "add_only": "Add new only",
        "update_only": "Update existing",
        "upsert": "Add + update",
    }.get(mode, mode)
    lines = [
        "📥 *Inventory file applied*",
        f"Mode: *{mode_label}*",
        f"Created: *{imported.created_count}*",
        f"Updated: *{imported.updated_count}*",
        f"Skipped: *{imported.skipped_count}*",
        f"Errors: *{len(parsed.errors) + len(imported.errors)}*",
    ]

    def _sample(label: str, names: list[str], emoji: str) -> None:
        if not names:
            return
        sample = ", ".join(names[:max_list])
        extra = len(names) - max_list
        lines.append(
            f"\n{emoji} {label}: {sample}"
            + (f" (+{extra} more)" if extra > 0 else "")
        )

    _sample("Added", imported.created, "✅")
    _sample("Updated", imported.updated, "🔄")
    _sample("Skipped", imported.skipped, "⏭")

    all_errs = list(parsed.errors) + list(imported.errors)
    if all_errs:
        lines.append("\n⚠️ Errors:")
        for e in all_errs[:12]:
            lines.append(f"• {e}")
        if len(all_errs) > 12:
            lines.append(f"• …and {len(all_errs) - 12} more")

    if (
        imported.created_count == 0
        and imported.updated_count == 0
        and imported.skipped_count == 0
        and all_errs
    ):
        lines.insert(1, "_Nothing changed._")
    return "\n".join(lines)
