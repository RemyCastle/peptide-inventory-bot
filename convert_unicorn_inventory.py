"""Convert Desktop UNicorn.txt price list → inventory bot import format."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(r"C:\Users\Remy\OneDrive\Desktop\UNicorn.txt")
# Read original first if already converted, use backup... we'll read as-is
DEFAULT_STOCK = 10

# If file already converted (has header), bail
def main() -> None:
    raw = SRC.read_text(encoding="utf-8", errors="replace")
    if raw.lstrip().startswith("# Unicorn inventory import"):
        print("Already in import format; regenerating from same path still ok if source mixed")
    # Prefer raw freeform: if looks like import already, still re-read
    # Save a backup of freeform if needed
    backup = Path(r"C:\Users\Remy\OneDrive\Desktop\UNicorn_original_backup.txt")
    if not raw.lstrip().startswith("# Unicorn inventory import"):
        backup.write_text(raw, encoding="utf-8")
        print(f"backup → {backup}")
    else:
        # restore from backup if present for re-parse
        if backup.exists():
            raw = backup.read_text(encoding="utf-8", errors="replace")
            print("re-parsing from backup")

    raw = raw.replace("\u00a0", " ").replace("\u2009", " ")
    # Normalize fancy dollars / spaces
    for ch in ("\uff04", "\uFE69", "\u00a2", "＄"):
        raw = raw.replace(ch, "$")
    # Some exports use " $" with weird chars before dollar-like amounts written as $ already
    lines_in = raw.splitlines()

    products: list[tuple[str, float, int, str, str]] = []
    seen: set[str] = set()

    def add(
        name: str,
        price: float | str,
        unit: str = "vial",
        desc: str = "",
        stock: int = DEFAULT_STOCK,
    ) -> None:
        name = re.sub(r"\s+", " ", str(name)).strip(" -\t–—")
        if not name or re.search(r"coming soon", name, re.I):
            return
        # junk name tails
        name = re.sub(r"\s+10 for$", "", name, flags=re.I).strip()
        if len(name) > 120:
            name = name[:117] + "..."
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return
        if price_f <= 0:
            return
        unit = (unit or "vial").strip().lower() or "vial"
        if unit in ("ampule", "amp"):
            unit = "ampoule"
        # Prefer non-vial unit when same name+price already as vial from mis-parse
        key_price = f"{name.casefold()}|{price_f:.2f}"
        key = f"{name.casefold()}|{unit}|{price_f:.2f}"
        if key in seen:
            return
        # Drop weaker vial duplicate of ea/pack accessory
        if unit == "vial":
            for u2 in ("ea", "pack", "bottle", "tube", "kit", "syringe", "ampoule"):
                if f"{name.casefold()}|{u2}|{price_f:.2f}" in seen:
                    return
        if unit != "vial" and f"{name.casefold()}|vial|{price_f:.2f}" in seen:
            # remove prior vial mis-tag with same price
            products[:] = [
                p
                for p in products
                if not (
                    p[0].casefold() == name.casefold()
                    and p[3] == "vial"
                    and abs(p[1] - price_f) < 0.001
                )
            ]
            seen.discard(f"{name.casefold()}|vial|{price_f:.2f}")
        seen.add(key)
        seen.add(key_price + f"|{unit}")
        desc = re.sub(r"\s+", " ", desc).strip(" ,;-")
        products.append((name, price_f, stock, unit, desc))

    skip_substrings = (
        "t.me/",
        "the topic",
        "was created",
        "unicorn board certified",
        "hey girl hey",  # handled separately
    )

    i = 0
    while i < len(lines_in):
        line = lines_in[i].strip()
        i += 1
        if not line:
            continue
        low = line.casefold()
        if any(s in low for s in skip_substrings):
            continue
        if low.startswith(">") or low.startswith("[7/") or low.startswith("[/"):
            continue
        if re.match(r"^v[123]\s", low) or low.startswith("white pen"):
            continue

        # Bioregs block
        if low.startswith("all bioregs"):
            m = re.search(r"\$(\d+(?:\.\d+)?)", line)
            bio_price = float(m.group(1)) if m else 22.0
            while i < len(lines_in):
                n = lines_in[i].strip()
                if not n:
                    i += 1
                    break
                nl = n.casefold()
                if "$" in n or nl.startswith("b12") or nl.startswith("topical"):
                    break
                add(n, bio_price, "vial", "bioreg")
                i += 1
            continue

        # hey girl hey + following pure "24iu $20/vial" lines
        if low.startswith("hey girl hey") or re.match(
            r"^\d+(?:\.\d+)?\s*iu\b", low
        ):
            m = re.search(
                r"(?:hey girl hey\s*)?(\d+(?:\.\d+)?\s*iu).*?\$(\d+(?:\.\d+)?)",
                line,
                re.I,
            )
            if m:
                add(
                    f"Hey girl hey {m.group(1).replace(' ', '')}",
                    float(m.group(2)),
                    "vial",
                )
            if low.startswith("hey girl hey"):
                while i < len(lines_in):
                    n = lines_in[i].strip().replace("\u00a0", " ")
                    if not n:
                        break
                    m2 = re.match(
                        r"^(\d+(?:\.\d+)?\s*iu)\s*\$(\d+(?:\.\d+)?)",
                        n,
                        re.I,
                    )
                    if not m2:
                        break
                    add(
                        f"Hey girl hey {m2.group(1).replace(' ', '')}",
                        float(m2.group(2)),
                        "vial",
                    )
                    i += 1
            continue

        # Normalize any dollar-like glyphs on this line
        line_norm = line
        for ch in ("\uff04", "\uFE69", "＄", "﹩"):
            line_norm = line_norm.replace(ch, "$")
        # Unicode spaces before amounts
        line_norm = re.sub(r"[\u00a0\u2000-\u200b\u202f\u205f\u3000]+", " ", line_norm)

        if "coming soon" in low and not re.search(r"\$\s*\d", line_norm):
            continue
        if "$" not in line_norm:
            continue

        notes: list[str] = []
        if re.search(r"testing pending|test pending", line_norm, re.I):
            notes.append("testing pending")
        if re.search(r"more on the way", line_norm, re.I):
            notes.append("more on the way")
        if re.search(r"limiting to 3|shortage", line_norm, re.I):
            notes.append("limit 3 / shortage")
        if re.search(r"2 day shipping|ice packs", line_norm, re.I):
            notes.append("2-day ship + ice packs")
        if re.search(r"refrigerate", line_norm, re.I):
            notes.append("refrigerate")

        work = line_norm
        work = re.sub(
            r"\s*[-–—]\s*(testing pending|test pending|more on the way.*|"
            r"currently limiting.*|requires 2 day.*)",
            "",
            work,
            flags=re.I,
        )
        work = re.sub(r"\s*testing pending", "", work, flags=re.I)

        # "10 for $5" / "10mg 10 for $7.50"
        m_for = re.search(
            r"^(?:(\d+\s*mg)\s+)?(\d+)\s+for\s+\$(\d+(?:\.\d+)?)", work, re.I
        )
        if m_for and "prednisone" not in work.casefold():
            # bare continuation lines for prednisone already handled below
            pass

        # Prednisone loose tabs style on own lines
        m_pred = re.match(
            r"^(\d+\s*mg)\s+(\d+)\s+for\s+\$(\d+(?:\.\d+)?)", work, re.I
        )
        if m_pred:
            add(
                f"Prednisone {m_pred.group(1).replace(' ', '')}",
                float(m_pred.group(3)),
                "pack",
                f"{m_pred.group(2)} loose tabs",
            )
            continue

        m_pred2 = re.match(
            r"^(\d+)\s+for\s+\$(\d+(?:\.\d+)?)\s*\(?loose", work, re.I
        )
        if m_pred2:
            add("Prednisone 10mg", float(m_pred2.group(2)), "pack", f"{m_pred2.group(1)} loose tabs")
            continue

        m_pred3 = re.match(r"^(\d+)\s+for\s+\$(\d+(?:\.\d+)?)\s*$", work)
        if m_pred3 and i > 1:
            # could be "10 for $5" after Prednisone line
            prev = lines_in[i - 2].strip().casefold() if i >= 2 else ""
            if "prednisone" in prev or "prednisone" in work.casefold():
                add("Prednisone 5mg", float(m_pred3.group(2)), "pack", f"{m_pred3.group(1)} tabs")
                continue

        # name before first $
        if "$" not in work:
            continue
        name_part, rest = work.split("$", 1)
        name_part = name_part.strip(" -–—")
        rest = "$" + rest
        if not name_part:
            # "$5" only lines unlikely
            continue

        # Collect $price with optional /unit
        found: list[tuple[float, str]] = []
        for m in re.finditer(
            r"\$(\d+(?:\.\d+)?)\s*(?:/\s*(vial|kit|tube|bottle|pack|ea|ampoule|ampule|syringe)|"
            r"(?:\s+(vial|kit|tube|bottle)\b))?",
            rest,
            re.I,
        ):
            price = float(m.group(1))
            unit = (m.group(2) or m.group(3) or "").lower()
            # look nearby for kit/vial words
            span = rest[max(0, m.start() - 12) : m.end() + 12].lower()
            if not unit:
                if re.search(r"\bkit\b", span):
                    unit = "kit"
                elif re.search(r"\bvial\b", span):
                    unit = "vial"
            found.append((price, unit))

        if not found:
            m = re.search(r"\$(\d+(?:\.\d+)?)", rest)
            if m:
                u = "vial"
                if re.search(r"\bkit\b", work, re.I):
                    u = "kit"
                elif re.search(r"\btube\b", work, re.I):
                    u = "tube"
                elif re.search(r"\bbottle\b", work, re.I):
                    u = "bottle"
                elif re.search(r"tincture|capsules|caps\b", work, re.I):
                    u = "bottle"
                elif re.search(r"ampoule|ampule", work, re.I):
                    u = "ampoule"
                desc = "; ".join(notes)
                extra = re.sub(r"\$\d+(?:\.\d+)?(?:/\w+)?", "", rest)
                extra = re.sub(r"\s+", " ", extra).strip(" -–,")
                if extra and len(extra) > 2:
                    desc = f"{desc}; {extra}".strip("; ")
                add(name_part, float(m.group(1)), u, desc)
            continue

        # Fill empty units when two prices (kit + vial)
        if len(found) >= 2:
            units = [u for _, u in found if u]
            filled: list[tuple[float, str]] = []
            for p, u in found:
                if u:
                    filled.append((p, u))
                elif "vial" in units:
                    # higher price → kit usually
                    other_vial = min(x[0] for x in found if x[1] == "vial" or not x[1])
                    filled.append((p, "kit" if p > other_vial else "vial"))
                elif "kit" in units:
                    filled.append((p, "vial"))
                else:
                    # two bare prices: higher = kit, lower = vial if "or" pattern
                    filled.append((p, ""))
            # second pass empty
            prices_only = [p for p, _ in filled]
            if any(not u for _, u in filled) and len(filled) == 2:
                hi, lo = max(prices_only), min(prices_only)
                filled = [
                    (p, "kit" if p == hi and hi != lo else ("vial" if p == lo else "vial"))
                    for p, u in filled
                ]
            found = [(p, u or "vial") for p, u in filled]
        else:
            p, u = found[0]
            if not u:
                if re.search(r"\bkit\b", work, re.I) and not re.search(
                    r"/vial|per vial|\bvial\b", work, re.I
                ):
                    u = "kit"
                elif re.search(r"\btube\b", work, re.I):
                    u = "tube"
                elif re.search(r"\bbottle\b", work, re.I):
                    u = "bottle"
                elif re.search(r"tincture|capsules", work, re.I):
                    u = "bottle"
                elif re.search(r"ampoule|ampule", work, re.I):
                    u = "ampoule"
                elif re.search(r"syringe", work, re.I):
                    u = "syringe"
                else:
                    u = "vial"
            found = [(p, u)]

        desc_base = "; ".join(notes)
        # residual description (strength notes etc.)
        extra = rest
        for p, u in found:
            extra = re.sub(
                rf"\${p:g}(?:/\s*{u})?", "", extra, count=1, flags=re.I
            ) if False else extra
        extra = re.sub(r"\$\d+(?:\.\d+)?(?:/\s*\w+)?", "", rest)
        extra = re.sub(r"\bor\b", " ", extra, flags=re.I)
        extra = re.sub(r"\s+", " ", extra).strip(" -–,/")
        if extra and len(extra) > 2 and not re.match(r"^(kit|vial)$", extra, re.I):
            desc_base = f"{desc_base}; {extra}".strip("; ")

        for p, u in found:
            add(name_part, p, u, desc_base)

    # Special fixed lines often missed
    specials = [
        ("Pen (assorted colors)", 15, "ea", "V1/V2/V3 colors"),
        ("Pen cartridges", 1, "ea", "tested sterile"),
        ("Pen noodles (needles)", 5, "pack", "25 count 33g/32g"),
    ]
    for name, price, unit, desc in specials:
        if any(name.casefold() in p[0].casefold() for p in products):
            continue
        # only add if mentioned in source
        if name.split()[0].casefold() in raw.casefold():
            add(name, price, unit, desc)

    # 3D / accessory lines with trailing price
    for line in lines_in:
        line = line.strip().replace("\u00a0", " ")
        low = line.casefold()
        if any(
            k in low
            for k in (
                "slide case",
                "single vial container",
                "flexi caps",
                "purge blocks",
                "spike holder",
                "3ml case",
                "10ct",
                "7ct",
                "bac with vial",
            )
        ):
            m = re.search(r"(.+?)\s+\$(\d+(?:\.\d+)?)\s*$", line)
            if m:
                add(m.group(1).strip(), float(m.group(2)), "ea", "3d / accessory")

    # Pens line
    for line in lines_in:
        low = line.strip().casefold()
        if low.startswith("pens $"):
            m = re.search(r"\$(\d+)", line)
            if m:
                add("Injection pen", float(m.group(1)), "ea", "color options V1/V2/V3")
        if low.startswith("pen carts"):
            m = re.search(r"\$(\d+)", line)
            if m:
                add("Pen cartridges", float(m.group(1)), "ea", "tested sterile")
        if low.startswith("pen noodles"):
            m = re.search(r"\$(\d+)", line)
            if m:
                add("Pen noodles (needles)", float(m.group(1)), "pack", "25 count")

    products.sort(key=lambda x: (x[0].casefold(), x[3], x[1]))

    out_lines = [
        "# Unicorn inventory import — Telegram inventory bot",
        "# Source: Desktop UNicorn.txt price list",
        "# Format: name | price | stock | unit | description",
        f"# Stock = {DEFAULT_STOCK} placeholder when not on the list (edit real stock after import).",
        "# When both kit and vial prices existed, two products were created (unit kit vs vial).",
        "# Skipped: coming soon / no price / chat noise.",
        "",
        "name | price | stock | unit | description",
    ]
    for name, price, stock, unit, desc in products:
        name = name.replace("|", "/")
        desc = desc.replace("|", "/") if desc else ""
        out_lines.append(f"{name} | {price:.2f} | {stock} | {unit} | {desc}")

    text = "\n".join(out_lines) + "\n"
    targets = [
        Path(r"C:\Users\Remy\OneDrive\Desktop\UNicorn.txt"),
        Path(r"C:\Users\Remy\OneDrive\Desktop\unicorn_inventory_import.txt"),
        Path(r"C:\Users\Remy\Desktop\unicorn_inventory_import.txt"),
    ]
    for t in targets:
        try:
            t.write_text(text, encoding="utf-8")
            print(f"wrote {t} ({len(products)} products)")
        except OSError as e:
            print(f"skip {t}: {e}")

    # Bot max 200 products per upload — split parts
    def write_part(path: Path, chunk: list, part_no: int, total_parts: int) -> None:
        hdr = [
            f"# Unicorn inventory import — part {part_no}/{total_parts}",
            "# Format: name | price | stock | unit | description",
            f"# Upload separately (bot max 200 products per file).",
            "",
            "name | price | stock | unit | description",
        ]
        body = []
        for name, price, stock, unit, desc in chunk:
            name = name.replace("|", "/")
            desc = (desc or "").replace("|", "/")
            body.append(f"{name} | {price:.2f} | {stock} | {unit} | {desc}")
        path.write_text("\n".join(hdr + body) + "\n", encoding="utf-8")
        print(f"wrote {path} ({len(chunk)} products)")

    max_per = 200
    parts = [products[i : i + max_per] for i in range(0, len(products), max_per)]
    for idx, chunk in enumerate(parts, start=1):
        for base in (
            Path(r"C:\Users\Remy\OneDrive\Desktop"),
            Path(r"C:\Users\Remy\Desktop"),
        ):
            try:
                write_part(
                    base / f"unicorn_inventory_import_part{idx}.txt",
                    chunk,
                    idx,
                    len(parts),
                )
            except OSError as e:
                print("skip part", e)

    # validate with bot parser if available
    try:
        import sys

        sys.path.insert(0, r"C:\Users\Remy\peptide_inventory_bot")
        from inventory_import import parse_inventory_text

        for idx, chunk in enumerate(parts, start=1):
            p = Path(r"C:\Users\Remy\OneDrive\Desktop") / f"unicorn_inventory_import_part{idx}.txt"
            r = parse_inventory_text(p.read_text(encoding="utf-8"))
            print(f"part{idx} parser: rows={len(r.rows)} errors={len(r.errors)}")
            for e in r.errors[:5]:
                print(" ERR", e)
    except Exception as e:
        print("parser skip", e)


if __name__ == "__main__":
    main()
