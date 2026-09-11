"""Make kit/vial product names unique so inventory bot import won't skip them."""
from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"C:\Users\Remy\peptide_inventory_bot")
import db  # noqa: E402
import inventory_import as inv  # noqa: E402


def fix_file(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[tuple[str, str, str, str, str]] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.lower().startswith("name |"):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) < 4:
            continue
        name, price, stock, unit = parts[0], parts[1], parts[2], parts[3]
        desc = " | ".join(parts[4:]) if len(parts) > 4 else ""
        rows.append((name, price, stock, unit, desc))

    name_counts = Counter(r[0].casefold() for r in rows)
    fixed: list[tuple[str, str, str, str, str]] = []
    for name, price, stock, unit, desc in rows:
        unit_l = (unit or "vial").strip().lower() or "vial"
        new_name = name
        if name_counts[name.casefold()] > 1:
            suffix = f" ({unit_l})"
            if not name.casefold().endswith(suffix.casefold()):
                new_name = f"{name}{suffix}"
        fixed.append((new_name, price, stock, unit_l, desc))

    # Still-duped names (same unit twice): append price
    c2 = Counter(r[0].casefold() for r in fixed)
    final: list[tuple[str, str, str, str, str]] = []
    for name, price, stock, unit, desc in fixed:
        if c2[name.casefold()] > 1:
            name = f"{name} ${price}"
        final.append((name, price, stock, unit, desc))

    header = [
        "# Unicorn inventory import — unique names for kit vs vial",
        "# Format: name | price | stock | unit | description",
        "# Duplicate base names got (kit)/(vial) so Import add-only will not skip them.",
        "# Stock placeholder 10 — set real stock after import.",
        "",
        "name | price | stock | unit | description",
    ]
    body = []
    for name, price, stock, unit, desc in final:
        name = name.replace("|", "/")[:120]
        desc = (desc or "").replace("|", "/")
        body.append(f"{name} | {price} | {stock} | {unit} | {desc}")
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    uniq = len({r[0].casefold() for r in final})
    print(f"{path.name}: {len(final)} rows, unique names={uniq}")


def main() -> None:
    bases = [
        Path(r"C:\Users\Remy\OneDrive\Desktop"),
        Path(r"C:\Users\Remy\Desktop"),
    ]
    for base in bases:
        for part in (1, 2):
            p = base / f"unicorn_inventory_import_part{part}.txt"
            if p.exists():
                fix_file(p)
        for name in ("UNicorn.txt", "unicorn_inventory_import.txt"):
            p = base / name
            if not p.exists():
                continue
            head = p.read_text(encoding="utf-8")[:400]
            if "name | price" in head or "stock | unit" in head:
                fix_file(p)

    # Simulate full add_only import of both parts
    tmp = tempfile.TemporaryDirectory()
    db.set_db_path(Path(tmp.name) / "t.db")
    db.init_db()
    db.ensure_shop(1)
    total_created = 0
    total_skipped = 0
    for part in (1, 2):
        t = Path(r"C:\Users\Remy\OneDrive\Desktop") / f"unicorn_inventory_import_part{part}.txt"
        text = t.read_text(encoding="utf-8")
        parsed = inv.parse_inventory_text(text)
        imp = inv.import_products(1, parsed.rows, mode="add_only")
        total_created += imp.created_count
        total_skipped += imp.skipped_count
        print(
            f"part{part}: parse={len(parsed.rows)} err={len(parsed.errors)} "
            f"created={imp.created_count} skipped={imp.skipped_count}"
        )
        for e in parsed.errors[:5]:
            print(" ERR", e)
    print("shop product count", len(db.list_products(1)))
    print("total created", total_created, "skipped", total_skipped)
    tmp.cleanup()


if __name__ == "__main__":
    main()
