"""Map supplier spellings onto one canonical SKU.

Known shop shorthand without a strength uses that product's usual strength:
bare tirzepatide / tirzepitide is TIRZ-30, and 5-Amino-1MQ with no milligrams
is 5-AMINO-1MQ-50. A written strength always wins, so Tirzepatide 20 stays
TIRZ-20. Unknown names return None.
"""

from __future__ import annotations

import re

_TIRZ_DEFAULT_MG = 30
_AMINO_DEFAULT_MG = 50


def _prep(name: str) -> str:
    text = str(name or "").lower().replace("–", "-").replace("—", "-")
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9+\-\s]", " ", text)
    text = text.replace("tirzepitide", "tirzepatide")
    text = text.replace("tirzepatide", "tirz")
    text = text.replace("retatrutide", "reta")
    text = re.sub(r"\bx\s*\d+\b", " ", text)
    return " ".join(text.split())


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text)


def _mg(text: str, drop: str) -> int | None:
    rest = re.sub(drop, " ", text, flags=re.IGNORECASE)
    rest = re.sub(r"\b(mg|mcg|iu|ml)\b", " ", rest)
    nums = [int(n) for n in re.findall(r"\d+", rest)]
    if not nums:
        return None
    return nums[-1]


def _with_dose(prefix: str, dose: int | None, default: int | None = None) -> str:
    chosen = dose if dose is not None else default
    if chosen is None:
        return prefix
    return f"{prefix}-{chosen}"


def canonical_sku(name: str) -> str | None:
    """Return a canonical SKU, or None when the name is not a known alias."""
    text = _prep(name)
    if not text:
        return None
    compact = _compact(text)

    shorthand = re.fullmatch(r"([rt])(\d{1,3})(?:mg)?", compact)
    if shorthand:
        prefix = "RETA" if shorthand.group(1) == "r" else "TIRZ"
        return f"{prefix}-{int(shorthand.group(2))}"

    if "amino1mq" in compact or "5amino1mq" in compact:
        dose = _mg(text, r"5\s*-?\s*amino\s*-?\s*1\s*mq")
        return _with_dose("5-AMINO-1MQ", dose, _AMINO_DEFAULT_MG)

    if "motsc" in compact:
        return _with_dose("MOTS-C", _mg(text, r"mots\s*-?\s*c"))

    if "ghkcu" in compact:
        return _with_dose("GHK-CU", _mg(text, r"ghk\s*-?\s*cu"))

    if re.search(r"\breta\b", text) or compact.startswith("reta"):
        return _with_dose("RETA", _mg(text, r"\breta\b"))

    if "tirz" in compact:
        return _with_dose("TIRZ", _mg(text, r"\btirz\w*\b"), _TIRZ_DEFAULT_MG)

    if re.search(r"\bnad\b", text) or re.fullmatch(r"nad\d{0,4}(?:mg)?", compact):
        return _with_dose("NAD", _mg(text, r"\bnad\b"))

    return None
