"""Price extraction: local Ollama first, deterministic regex fallback.

Supplier messages are private business data — parsing stays on-machine
(Ollama at 127.0.0.1). No cloud calls in this module.
"""

from __future__ import annotations

import json
import re
import urllib.request

from . import config

# ---------------------------------------------------------------- regex layer

_CURRENCY = {"$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP"}

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|iu|ml)\b", re.IGNORECASE)

# $45 | 45$ | 45.50 USD | €60
_PRICE_SYM_RE = re.compile(
    r"(?:(?P<cur1>[$€£])\s*(?P<amt1>\d{1,5}(?:[.,]\d{1,2})?))"
    r"|(?:(?P<amt2>\d{1,5}(?:[.,]\d{1,2})?)\s*(?P<cur2>[$€£]|(?:usd|eur|gbp)\b))",
    re.IGNORECASE,
)
# 'Tirzepatide 30mg .... 120' — bare number after a separator run, end of line
_PRICE_BARE_RE = re.compile(r"(?:[-–—:.]|\s){2,}(\d{2,5}(?:[.,]\d{1,2})?)\s*$")

_CLEAN_RE = re.compile(r"[^\w\s+-]|_", re.UNICODE)


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def normalize_key(product: str, size: str | None) -> str:
    p = re.sub(r"[^A-Z0-9]", "", product.upper())
    z = re.sub(r"\s", "", (size or "").upper())
    return f"{p}|{z}" if z else p


def regex_parse_prices(text: str) -> list[dict]:
    """Extract {product, size, price, currency} rows from messy price-list text."""
    items: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not any(c.isdigit() for c in line):
            continue

        price = currency = None
        m = _PRICE_SYM_RE.search(line)
        if m:
            price = _to_float(m.group("amt1") or m.group("amt2"))
            cur = (m.group("cur1") or m.group("cur2") or "$").lower()
            currency = _CURRENCY.get(cur, "USD")
            rest = line[: m.start()] + line[m.end():]
        else:
            m = _PRICE_BARE_RE.search(line)
            if not m:
                continue
            price, currency = _to_float(m.group(1)), "USD"
            rest = line[: m.start()]

        size = None
        sm = _SIZE_RE.search(rest)
        if sm:
            size = f"{sm.group(1)}{sm.group(2).lower()}"
            rest = rest[: sm.start()] + rest[sm.end():]

        product = _CLEAN_RE.sub(" ", rest)
        product = re.sub(r"\s{2,}", " ", product).strip(" -–—:.()+")
        if not product or price is None or price <= 0:
            continue
        items.append({"product": product, "size": size, "price": price,
                      "currency": currency})
    return items


# ----------------------------------------------------------------- LLM layer

_SYSTEM = (
    "You extract peptide prices from a supplier's Telegram message. "
    'Return ONLY JSON: {"items":[{"product":"BPC-157","size":"5mg","price":45.0,'
    '"currency":"USD"}]}. Rules: one item per product+size+price actually stated; '
    "size null if absent; currency from symbols/words else USD; ignore MOQ text, "
    "shipping, chatter; if the message contains no prices return {\"items\":[]}."
)


def ollama_parse_prices(text: str) -> list[dict]:
    """Parse via local Ollama. Raises on transport/format errors (caller falls back)."""
    body = json.dumps({
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 1024},
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": text[:6000]}],
    }).encode()
    req = urllib.request.Request(
        f"{config.OLLAMA_URL}/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT) as resp:
        data = json.loads(resp.read())
    parsed = json.loads(data["message"]["content"])
    items = parsed.get("items")
    if not isinstance(items, list):
        raise ValueError("model returned no items list")

    out = []
    for it in items:
        try:
            price = float(it["price"])
            product = str(it["product"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not product or price <= 0:
            continue
        size = it.get("size")
        out.append({
            "product": product,
            "size": str(size).replace(" ", "").lower() if size else None,
            "price": price,
            "currency": str(it.get("currency") or "USD").upper()[:3],
        })
    return out


def parse_message(text: str) -> tuple[list[dict], str]:
    """Returns (items, status). Status: llm_ok | regex_ok | no_prices | failed."""
    regex_items = regex_parse_prices(text)
    if config.PARSE_MODE != "regex_only":
        try:
            llm_items = ollama_parse_prices(text)
            # Trust the LLM unless it found nothing where regex clearly did
            if llm_items or not regex_items:
                items = llm_items
                status = "llm_ok" if llm_items else "no_prices"
            else:
                items, status = regex_items, "regex_ok"
        except Exception:
            items = regex_items
            status = "regex_ok" if regex_items else "failed"
    else:
        items = regex_items
        status = "regex_ok" if regex_items else "no_prices"

    for it in items:
        it["product_key"] = normalize_key(it["product"], it.get("size"))
    return items, status
