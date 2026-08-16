"""Parser tests over synthetic supplier-message fixtures (fake names/prices).

Run:  python -m supplier_watch.test_parser         (regex layer, no Ollama)
      python -m supplier_watch.test_parser --llm   (also exercise Ollama parse)
"""

from __future__ import annotations

import sys

from .parser import normalize_key, ollama_parse_prices, regex_parse_prices

FIXTURES = [
    ("bullets_dollar",
     "🔥 RESTOCK 🔥\n• BPC-157 5mg - $45\n• TB-500 10mg - $60\n• GHK-Cu 50mg - $70",
     [("BPC-157", "5mg", 45.0, "USD"), ("TB-500", "10mg", 60.0, "USD"),
      ("GHK-Cu", "50mg", 70.0, "USD")]),
    ("dotted_table",
     "Tirzepatide 30mg .... 120\nRetatrutide 10mg .... 95\nSemaglutide 5mg .... 55",
     [("Tirzepatide", "30mg", 120.0, "USD"), ("Retatrutide", "10mg", 95.0, "USD"),
      ("Semaglutide", "5mg", 55.0, "USD")]),
    ("word_currency",
     "Semaglutide 10 mg: 85 USD\nCJC-1295 no DAC 2mg: 30 USD",
     [("Semaglutide", "10mg", 85.0, "USD"), ("CJC-1295 no DAC", "2mg", 30.0, "USD")]),
    ("trailing_symbol",
     "TB500 (10mg) — 60$\nIpamorelin (5mg) — 25$",
     [("TB500", "10mg", 60.0, "USD"), ("Ipamorelin", "5mg", 25.0, "USD")]),
    ("euro",
     "EU stock!\nBPC-157 10mg €55\nTesamorelin 10mg €80",
     [("BPC-157", "10mg", 55.0, "EUR"), ("Tesamorelin", "10mg", 80.0, "EUR")]),
    ("chatter_no_prices",
     "Bro shipment landed today, list drops tomorrow around 5pm", []),
    ("mixed_chatter",
     "Good news: kits back in stock!\nTirzepatide 60mg $210\nDM to order, ships Monday",
     [("Tirzepatide", "60mg", 210.0, "USD")]),
]


def run_regex() -> int:
    failures = 0
    for name, text, expected in FIXTURES:
        got = regex_parse_prices(text)
        got_t = [(i["product"], i["size"], i["price"], i["currency"]) for i in got]
        if got_t != expected:
            failures += 1
            print(f"FAIL {name}\n  expected {expected}\n  got      {got_t}")
        else:
            print(f"ok   {name} ({len(got)} items)")
    assert normalize_key("BPC 157", "5mg") == "BPC157|5MG"
    assert normalize_key("bpc-157", "5 MG") == "BPC157|5MG"
    print("ok   normalize_key")
    return failures


def run_llm() -> int:
    failures = 0
    for name, text, expected in FIXTURES:
        items = ollama_parse_prices(text)
        prices = sorted(i["price"] for i in items)
        want = sorted(e[2] for e in expected)
        status = "ok  " if prices == want else "FAIL"
        failures += status == "FAIL"
        print(f"{status} llm:{name} want={want} got={prices}")
    return failures


if __name__ == "__main__":
    failed = run_regex()
    if "--llm" in sys.argv:
        failed += run_llm()
    print("PASS" if failed == 0 else f"{failed} FAILURES")
    sys.exit(1 if failed else 0)
