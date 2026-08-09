r"""
Regenerate a mini-app store's PRODUCTS array from a shop in inventory.db.

Reads active products for --chat-id and rewrites the block between
  //PRODUCTS-START  and  //PRODUCTS-END
in the store's index.html. Prices, kit prices and stock all come from the DB;
the store never invents numbers. Run this after any restock, then redeploy.

Usage:
  venv\Scripts\python.exe gen_store_catalog.py --chat-id -5551234567 ^
      --store C:\Users\Remy\projects\unicorn-miniapp-sample\index.html
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "inventory.db"

CATEGORY_RULES = [
    ("GLP-1",     ["sema", "tirz", "reta", "cagri", "glp"]),
    ("Recovery",  ["bpc", "tb-500", "tb500", "kpv", "dsip", "wolverine", "ss31", "ss-31"]),
    ("Longevity", ["nad", "mots", "ghk", "epitalon", "5-amino", "thymulin", "gluta", "vitamin"]),
    ("Blends",    ["klow", "blend", "cjc", "ipa", "mix", "stack"]),
]

TAGS = {
    "GLP-1": "the slimming spell",
    "Recovery": "the mending charm",
    "Longevity": "cellular pixie dust",
    "Blends": "the ultimate potion",
    "Other": "certified magical",
}

HUES = [0, 40, 150, 300, 200, 100, 60, 230, 70, 170, 260, 20]


def categorize(name: str) -> str:
    low = name.lower()
    for cat, keys in CATEGORY_RULES:
        if any(k in low for k in keys):
            return cat
    return "Other"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", type=int, required=True)
    ap.add_argument("--store", required=True, help="path to the store index.html")
    ap.add_argument("--include-zero-stock", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, name, price, kit_price, stock
        FROM products
        WHERE chat_id = ? AND active = 1
        ORDER BY sort_order, name
        """,
        (args.chat_id,),
    ).fetchall()

    products = []
    cats_seen: list[str] = []
    for i, r in enumerate(rows):
        stock = int(r["stock"] or 0)
        if stock <= 0 and not args.include_zero_stock:
            continue
        cat = categorize(r["name"])
        if cat not in cats_seen:
            cats_seen.append(cat)
        entry = {
            "id": int(r["id"]),
            "name": r["name"],
            "cat": cat,
            "price": float(r["price"]),
            "kitPrice": (float(r["kit_price"]) if r["kit_price"] else None),
            "stock": stock,
            "img": "bundle.jpg" if cat == "Blends" else "potion.jpg",
            "hue": HUES[i % len(HUES)],
            "tag": TAGS[cat],
        }
        products.append(entry)

    if not products:
        raise SystemExit(f"No sellable products found for chat_id {args.chat_id} - nothing written.")

    order = ["GLP-1", "Recovery", "Longevity", "Blends", "Other"]
    # NOTE: the store strips the leading emoji to get the filter key, so the
    # text after the first space must exactly match the product's cat value.
    labels = {"GLP-1": "✨ GLP-1", "Recovery": "🩹 Recovery", "Longevity": "🌙 Longevity",
              "Blends": "🧪 Blends", "Other": "💖 Other"}
    cats = ["🌈 All"] + [labels[c] for c in sorted(cats_seen, key=order.index)]

    lines = ",\n".join("  " + json.dumps(p, ensure_ascii=False) for p in products)
    block = (
        "//PRODUCTS-START (generated from inventory.db - do not hand-edit)\n"
        f"let PRODUCTS = [\n{lines}\n];\n"
        f"let CATS = {json.dumps(cats, ensure_ascii=False)};\n"
        "//PRODUCTS-END"
    )

    store = Path(args.store)
    html = store.read_text(encoding="utf-8")
    pat = re.compile(r"//PRODUCTS-START.*?//PRODUCTS-END", re.DOTALL)
    if not pat.search(html):
        raise SystemExit(f"markers not found in {store} - add //PRODUCTS-START and //PRODUCTS-END first")
    store.write_text(pat.sub(lambda _: block, html), encoding="utf-8")
    print(f"wrote {len(products)} products, cats {cats} -> {store}")


if __name__ == "__main__":
    main()
