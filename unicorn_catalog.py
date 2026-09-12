"""Unicorn Magic Factory catalog UX: categories, Title Case names, copy, art.

Shop-scoped. Never touches price, kit_price, stock, or active. Never DELETE.
Inventory prices stay the ministore source of truth.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from catalog_cleanup import clean_product_name, display_product_name, storefront_label

STATIC_DIR = Path(__file__).resolve().parent / "static" / "catalog"
_SLUG_RE = re.compile(r"^[a-z0-9-]+\.jpg$")
_IMG_URL_RE = re.compile(r"/catalog-img/[a-z0-9-]+\.jpg(?:\?.*)?$", re.I)

# Buyer chips — honest buckets, no catch-all "Other".
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("GLP-1", "✨ GLP-1", "the slimming spell"),
    ("Retatrutide", "🦄 Retatrutide", "triple-threat sparkle"),
    ("Healing", "🩹 Healing", "the mending charm"),
    ("Cognition", "🧠 Cognition", "brain-glitter hour"),
    ("Skin", "💅 Skin", "glow-up potion"),
    ("Metabolic", "🔥 Metabolic", "body-comp fizz"),
    ("Longevity", "🌙 Longevity", "cellular pixie dust"),
    ("Vitality", "💖 Vitality", "rainbow shelf"),
    ("Tabs", "💊 Tabs", "tiny treasure tabs"),
    ("BAC-water", "💧 BAC-water", "unicorn mixer"),
    ("Accessories", "🧰 Accessories", "factory gadgets"),
)
CATEGORY_IDS: tuple[str, ...] = tuple(c[0] for c in CATEGORIES)
CATEGORY_LABEL: dict[str, str] = {c[0]: c[1] for c in CATEGORIES}
CATEGORY_TAG: dict[str, str] = {c[0]: c[2] for c in CATEGORIES}
CATEGORY_IMAGE: dict[str, str] = {
    "GLP-1": "glp1",
    "Retatrutide": "reta",
    "Healing": "healing",
    "Cognition": "cognition",
    "Skin": "skin",
    "Metabolic": "metabolic",
    "Longevity": "longevity",
    "Vitality": "vitality",
    "Tabs": "tabs",
    "BAC-water": "bac",
    "Accessories": "accessories",
}

# Checked in order. More specific shelves first so "sema" cannot steal "semax".
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Accessories",
        (
            "slide case",
            "case holds",
            "flexi cap",
            "flexi caps",
            "vial container",
            "vial spike",
            "purge block",
            "injection pen",
            "pen cartridge",
            "pen cart",
            "pen noodles",
            "needleless",
            "needless",
            "applicator",
            "derma roller",
        ),
    ),
    (
        "BAC-water",
        (
            "bac water",
            "bac saline",
            "hospira",
            "ocean pharma",
            "unicorn tears",
            "aod water",
            "cagri water",
            "acetic acid",
            "phosphate buffer",
            "sterile saline",
            "sodium bicarbonate",
        ),
    ),
    (
        "Tabs",
        (
            "tabs",
            "capsules",
            "prednisone",
            "methylprednisolone",
            "anavar",
            "plan b",
            "levonorgestrel",
            "levonorgestral",
            "ethinylestradiol",
            "ethinyloestradial",
            "oral tada",
        ),
    ),
    (
        "Cognition",
        (
            "semax",
            "selank",
            "pinealon",
            "adamax",
            "p21",
            "pe 22",
            "pe-22",
            "pe 22-28",
            "dsip",
            "superhuman",
            "relax blend",
            "sleep blend",
            "vip",
        ),
    ),
    (
        "Skin",
        (
            "ghk",
            "ghkcu",
            "ahk",
            "ahkcu",
            "ahk-cu",
            "ahk cu",
            "glow",
            "klow",
            "snap 8",
            "snap-8",
            "synake",
            "matrixyl",
            "tret",
            "elasty",
            "dermaheal",
            "hair growth",
            "hair luma",
            "hair skin",
            "facial cream",
            "skin booster",
            "kabelline",
            "kopyrrol",
            "methylene blue",
        ),
    ),
    ("Retatrutide", ("reta",)),
    (
        "GLP-1",
        ("sema", "tirz", "cagri", "cag", "maz12", "mazdutide"),
    ),
    (
        "Healing",
        (
            "bpc",
            "tb500",
            "tb-500",
            "tb4",
            "tb frag",
            "kpv",
            "ss31",
            "ss-31",
            "ara 290",
            "aicar",
            "ll37",
            "ll-37",
            "ta1",
            "thymulin",
            "thymogen",
            "recovery blend",
            "joint support",
            "immunity",
            "hishiphagen",
            "laennec",
        ),
    ),
    (
        "Metabolic",
        (
            "aod",
            "hgh fragment",
            "fat blaster",
            "fat dissolver",
            "lipo",
            "lemon bottle",
            "peach bottle",
            "pine bottle",
            "shred",
            "shredder",
            "tesa",
            "tesamorelin",
            "cjc",
            "ipa",
            "sermorelin",
            "igf",
            "hey girl hey",
            "5a1",
            "5-amino",
            "5 amino",
            "slupp",
            "slu pp",
            "lcarn",
            "l-carn",
            "alcarnitine",
            "mic blend",
        ),
    ),
    (
        "Longevity",
        (
            "nad",
            "nmn",
            "mots",
            "motsc",
            "epitalon",
            "foxo4",
            "fox04",
            "glutathione",
            "cardiogen",
            "cartalax",
            "chonluten",
            "cortagen",
            "crystagen",
            "livagen",
            "ovagen",
            "pancragen",
            "prostamax",
            "vesilute",
            "vesugen",
            "vilon",
            "testagen",
        ),
    ),
    (
        "Vitality",
        (
            "b12",
            "vitamin",
            "multivitamin",
            "vit c",
            "vit d",
            "zinc",
            "ginko",
            "ginkgo",
            "maxiblue",
            "hcg",
            "hmg",
            "kisspeptin",
            "pt141",
            "pt-141",
            "oxytocin",
            "mt1",
            "mt2",
            "adrenaline",
            "lidocaine",
            "muchcaine",
            "thioctic",
            "chioctocin",
            "atp-s",
            "atp s",
            "atps",
            "lady test",
            "test e",
            "test cyp",
            "elora",
            "survo",
        ),
    ),
)

# Full-string aliases (cleaned, casefold) for leftover junk / overlong import names.
_NAME_ALIASES: dict[str, str] = {
    "tirz100": "Tirz 100",
    "tret.025": "Tret 0.025",
    "tret .05": "Tret 0.05",
    "snap 8": "Snap-8",
    "snap-8": "Snap-8",
    "snap- 8": "Snap-8",
    "ghk- cu": "GHK-Cu",
    "ghk-cu": "GHK-Cu",
    "ahk- cu": "AHK-Cu",
    "ss- 31 10mg": "SS-31 10mg",
    "ss- 31 25mg": "SS-31 25mg",
    "ss- 31 30mg": "SS-31 30mg",
    "pt- 141": "PT-141",
    "pt- 141 10mg": "PT-141 10mg",
    "tb- 500 (tb4) 10mg": "TB-500 (TB4) 10mg",
    "foxo4- dri 10": "FOXO4-DRI 10",
    "atp- s inj": "ATP-S Inj",
    "ll- 37 5mg": "LL-37 5mg",
    "pe 22- 28": "PE 22-28",
    "pe 22- 28 8mg": "PE 22-28 8mg",
    "igf- 1 lr3 1mg": "IGF-1 LR3 1mg",
    "5- amino- 1mq 100mg": "5-Amino-1MQ 100mg",
    "5- amino- 1mq 50mg": "5-Amino-1MQ 50mg",
    "5- amino- 1mq tabs": "5-Amino-1MQ Tabs",
    "5- amino- 1mq 100mg / ml 10ml": "5-Amino-1MQ 100mg/ml 10ml",
    "mots-c- c 10mg": "MOTS-c 10mg",
    "mots-c- c 20mg": "MOTS-c 20mg",
    "mots-c- c 30mg": "MOTS-c 30mg",
    "mots-c- c 40mg": "MOTS-c 40mg",
    "mots-c 10mg": "MOTS-c 10mg",
    "mots-c 20mg": "MOTS-c 20mg",
    "mots-c 30mg": "MOTS-c 30mg",
    "mots-c 40mg": "MOTS-c 40mg",
    "plan b": "Plan B",
    "5a1 tabs": "5-Amino-1MQ Tabs",
    "5a1 100mg": "5-Amino-1MQ 100mg",
    "5a1 50mg": "5-Amino-1MQ 50mg",
    "5 amino 1 100mg/ml 10ml": "5-Amino-1MQ 100mg/ml 10ml",
    "cjc no dac 10mg": "CJC (No DAC) 10mg",
    "cjc no dac 5mg": "CJC (No DAC) 5mg",
    "cjc + ipa 10/10": "CJC + IPA 10/10",
    "cjc + ipa 5/5": "CJC + IPA 5/5",
    "bpc/tb4 10/10": "BPC / TB4 10/10",
    "bpc/tb4 5/5": "BPC / TB4 5/5",
    "bpc/tb frag 10/10": "BPC / TB Frag 10/10",
    "ghk basic (white, no copper)": "GHK Basic (No Copper)",
    "tb500 (tb4) 10mg": "TB-500 (TB4) 10mg",
    "fox04 dri 10": "FOXO4-DRI 10",
    "pgb nad+ 485 apx": "PGB NAD+ ~485",
    "slupp injectible 7.5mg/ml 10ml": "SluPP Injectable 7.5mg/ml 10ml",
    "slupp oral tincture": "SluPP Oral Tincture",
    "slupp 50 capsules 22mg": "SluPP Capsules 22mg · 50ct",
    "bc (levonorgestral/ethinyloestradial)": "BC (Levonorgestrel / Ethinylestradiol)",
    "8mg methylprednisolone 14 tabs": "Methylprednisolone 8mg · 14 Tabs",
    "unicorn tears (cagri water ph 4.5)": "Unicorn Tears (Cagri Water pH 4.5)",
    "chioctocin (thioctic acid) 5ml ampule": "Thioctic Acid 5ml Ampoule",
    "ginko biloba 17.5mg/5ml ampoules": "Ginkgo Biloba 17.5mg/5ml Ampoules",
    "b12 5ml vial (hydroxycolabin 2mg/ml)": "B12 5ml Vial (Hydroxocobalamin 2mg/ml)",
    "ahkcu": "AHK-Cu",
    "ghkcu": "GHK-Cu",
    "motsc 30mg": "MOTS-c 30mg",
    "mots c 10mg": "MOTS-c 10mg",
    "mots 20mg": "MOTS-c 20mg",
    "mots 40mg": "MOTS-c 40mg",
    "glow 70mg": "Glow 70mg",
    "klow 80mg": "Klow 80mg",
    "cag 10": "Cagri 10",
    "cag 5": "Cagri 5",
    "igf-1lr3 1mg": "IGF-1 LR3 1mg",
    "igf des 2mg": "IGF DES 2mg",
    "hey girl hey 13.5iu": "Hey Girl Hey 13.5 IU",
    "hey girl hey 17iu": "Hey Girl Hey 17 IU",
    "hey girl hey 24iu": "Hey Girl Hey 24 IU",
    "pens": "Pens",
    "pen carts": "Pen Carts",
    "pen cartridges": "Pen Cartridges",
}

_NAME_PREFIX_ALIASES: tuple[tuple[str, str], ...] = (
    ("dermaheal hl", "Dermaheal HL 5ml"),
    ("hair growth serum", "Hair Growth Serum 7ml"),
    ("hair luma", "Hair Luma 5ml"),
    ("maxiblue", "Maxiblue Trace Elements"),
    ("multivitamin", "Multivitamin Ampoule"),
    ("nmn nad+", "NMN NAD+ Skin Booster 5ml"),
    ("ocean pharma bac", "Ocean Pharma BAC 30ml"),
    ("sodium bicarbonate", "Sodium Bicarbonate Buffer"),
    ("needless scalp", "Needleless Scalp Serum Applicator"),
    ("needleless scalp", "Needleless Scalp Serum Applicator"),
    ("mini pen cartridge purge", "Mini Pen Cartridge Purge Blocks"),
    ("bpc/kpv capsules", "BPC / KPV Capsules · 100ct"),
    ("kabelline", "Kabelline Contouring Serum"),
    ("muchcaine", "Muchcaine Lidocaine Cream 30g"),
    ("bac water (pfizer", "BAC Water (Pfizer / Hospira) 30ml"),
    ("bac with vial spike", "BAC Vial Spike Holder"),
    ("hgh fragment", "HGH Fragment 176–191 5mg"),
)

_TYPOS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bneedless\b", re.I), "Needleless"),
    (re.compile(r"\binjectible\b", re.I), "Injectable"),
    (re.compile(r"\bhydrochorate\b", re.I), "Hydrochloride"),
    (re.compile(r"\blevonorgestral\b", re.I), "Levonorgestrel"),
    (re.compile(r"\bethinyloestradial\b", re.I), "Ethinylestradiol"),
    (re.compile(r"\bhydroxycolabin\b", re.I), "Hydroxocobalamin"),
    (re.compile(r"\btirz100\b", re.I), "Tirz 100"),
    (re.compile(r"tret\.025", re.I), "Tret 0.025"),
    (re.compile(r"tret\s*\.05", re.I), "Tret 0.05"),
    (re.compile(r"\bfox04\b", re.I), "FOXO4"),
    (re.compile(r"\bampule\b", re.I), "Ampoule"),
    (re.compile(r"\bedamatane\b", re.I), "Edamathane"),
    (re.compile(r"\bapx\b", re.I), "~"),
)

_ABBREV = {
    "bpc": "BPC",
    "tb4": "TB4",
    "tb500": "TB-500",
    "ghk": "GHK",
    "ghkcu": "GHK-Cu",
    "ahkcu": "AHK-Cu",
    "nad": "NAD+",
    "nad+": "NAD+",
    "nmn": "NMN",
    "cjc": "CJC",
    "ipa": "IPA",
    "mt1": "MT1",
    "mt2": "MT2",
    "ss31": "SS-31",
    "igf": "IGF",
    "hgh": "HGH",
    "hcg": "HCG",
    "hmg": "HMG",
    "kpv": "KPV",
    "dsip": "DSIP",
    "aod": "AOD",
    "ara": "ARA",
    "aicar": "AICAR",
    "vip": "VIP",
    "pt141": "PT-141",
    "pbs": "PBS",
    "ta1": "TA1",
    "ll37": "LL-37",
    "mots": "MOTS",
    "motsc": "MOTS-c",
    "mots-c": "MOTS-c",
    "ghk-cu": "GHK-Cu",
    "ahk-cu": "AHK-Cu",
    "ss-31": "SS-31",
    "tb-500": "TB-500",
    "pt-141": "PT-141",
    "atp-s": "ATP-S",
    "ll-37": "LL-37",
    "snap-8": "Snap-8",
    "foxo4-dri": "FOXO4-DRI",
    "5-amino-1mq": "5-Amino-1MQ",
    "igf-1": "IGF-1",
    "igf-1lr3": "IGF-1 LR3",
    "foxo4": "FOXO4",
    "p21": "P21",
    "b12": "B12",
    "b1": "B1",
    "b2": "B2",
    "b3": "B3",
    "b6": "B6",
    "b7": "B7",
    "atp": "ATP",
    "atp-s": "ATP-S",
    "dac": "DAC",
    "na": "NA",
    "hl": "HL",
    "coa": "COA",
    "ph": "pH",
    "iu": "IU",
    "pdrn": "PDRN",
    "dna": "DNA",
    "mic": "MIC",
    "igf-1lr3": "IGF-1 LR3",
    "des": "DES",
    "dri": "DRI",
    "pm": "PM",
    "bc": "BC",
    "cag": "Cagri",
}

_SMALL = frozenset({"of", "for", "and", "with", "the", "a", "to", "or", "in", "no", "on"})
_UNITISH = re.compile(
    r"^\d+(?:\.\d+)?(?:mg|ml|mcg|iu|g|oz|ct|mg/ml)?$", re.I
)
_RATIO = re.compile(r"^\d+/\d+$")
# Do not split on hyphen: Snap-8, GHK-Cu, MOTS-c, TB-500 stay one token.
_TOKEN = re.compile(r"[^\s/·,;()+]+|[()/·,;+]")
_DISAMBIG_PRICE = re.compile(r"\s*(?:·\s*)?\(\s*\$\s*\d+(?:\.\d+)?\s*\)\s*$")
_KEEP_LOWER = frozenset({"mg", "ml", "mcg", "iu", "g", "oz", "ct"})

_FLAIR: dict[str, tuple[str, ...]] = {
    "GLP-1": (
        "Slim-spell sparkle for the research shelf — not a doctor's note.",
        "Pretty glass, Factory-fresh. Research-only unicorn chemistry.",
    ),
    "Retatrutide": (
        "Triple-threat glitter in a tiny glass castle. Research-only.",
        "The Factory's show pony. For the shelf, not a prescription pad.",
    ),
    "Healing": (
        "The mending charm — cute vial, serious sparkle, research-only.",
        "Bandage-colored magic from the Factory. Not medical advice.",
    ),
    "Cognition": (
        "Brain-glitter hour. For curious minds, not clinic claims.",
        "Star-cap sparkle for the thinky shelf. Research-only.",
    ),
    "Skin": (
        "Glow-up potion energy. Pretty, pearly, research-shelf only.",
        "Vanity-table magic from the Factory. Not a derm appointment.",
    ),
    "Metabolic": (
        "Body-comp fizz in a candy vial. Research-only, no fairy tales.",
        "The zippy shelf. Sparkle for the bench, not a meal plan.",
    ),
    "Longevity": (
        "Cellular pixie dust. Moon-cap magic, research-only.",
        "Long-game glitter from the Factory. Not a fountain of youth.",
    ),
    "Vitality": (
        "Rainbow-shelf charm. Cute, fizzy, research-only.",
        "Everyday Factory sparkle — we don't play doctor.",
    ),
    "Tabs": (
        "Tiny treasure tabs. Pocket magic, research-only.",
        "Little Factory jewels. Not a pharmacy run.",
    ),
    "BAC-water": (
        "The mixer that makes the magic pour. Factory-fresh.",
        "Unicorn tap water for reconstitution. Sparkle, not a claim.",
    ),
    "Accessories": (
        "Factory gadgets for a tidier magic shelf.",
        "Cute hardware so the vials travel in style.",
    ),
}

_SPECIAL_DESC: dict[str, str] = {
    "unicorn tears (cagri water ph 4.5)": (
        "Cagri's favorite mixer — pH 4.5 sparkle water from the Factory tap."
    ),
    "bac water 10ml": "The classic mixer. Factory-fresh BAC water, research-shelf only.",
    "bac water 2ml": "A pocket-size mixer vial. Small pour, full sparkle.",
}

# Specific first. _has keeps "sema" from stealing "semax".
_COMPOUND_BLURB: tuple[tuple[str, str], ...] = (
    (
        "ahk",
        "Copper tripeptide (AHK-Cu) on the skin-and-hair shelf. Research-only.",
    ),
    (
        "ghk basic",
        "Copper-free GHK on the skin-and-hair shelf. Research-only.",
    ),
    (
        "ghk",
        "Copper-blue GHK-Cu on the skin-and-hair shelf. Research-only.",
    ),
    (
        "atp-s",
        "ATP-S injectable on the cellular-energy shelf. Research-only.",
    ),
    (
        "atps",
        "ATP-S injectable on the cellular-energy shelf. Research-only.",
    ),
)


def _hay(name: str) -> str:
    s = display_product_name(name).casefold()
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s*-\s*", "-", s)
    return " ".join(s.split())


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def _has(hay: str, needle: str) -> bool:
    n = needle.casefold().strip()
    if not n:
        return False
    n = re.sub(r"\s*-\s*", "-", n)
    if re.search(r"[^a-z0-9]", n):
        if n in hay:
            return True
        cn = _compact(n)
        ch = _compact(hay)
        if not cn:
            return False
        return re.search(rf"(?<![a-z0-9]){re.escape(cn)}(?![a-z])", ch) is not None
    if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z])", hay):
        return True
    ch = _compact(hay)
    return re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z])", ch) is not None


def _categorize_one(name: str) -> str:
    """Honest shelf for one spelling. Never returns a junk 'Other' bucket."""
    hay = _hay(name)
    if not hay:
        return "Accessories"
    if re.search(r"\bpens?\b", hay) and not _has(hay, "pending"):
        if any(
            k in hay
            for k in ("pen cart", "pen noodle", "injection pen", "assorted")
        ) or hay in {"pen", "pens"}:
            return "Accessories"
    for cat, needles in _RULES:
        if any(_has(hay, n) for n in needles):
            return cat
    # Peptide-shaped leftovers (mg / ml / g / blend) live on Healing, not gadgets.
    if re.search(r"\b(\d+\s*)?(mg|mcg|ml|iu|g|blend|vial)\b", hay):
        return "Healing"
    return "Accessories"


def categorize(name: str) -> str:
    """Honest shelf. Uses Title Case spelling so AHK-Cu / ATP-S survive rename."""
    pretty = pretty_name(name) if name else ""
    for cand in (pretty, name):
        if not cand:
            continue
        cat = _categorize_one(cand)
        if cat != "Accessories":
            return cat
    return _categorize_one(pretty or name or "")


def _fix_typos(text: str) -> str:
    s = text
    for pat, repl in _TYPOS:
        s = pat.sub(repl, s)
    return s


def _title_token(tok: str, index: int, total: int) -> str:
    if tok in "/()·,;+-":
        return tok
    raw = tok
    low = tok.casefold()
    if low in _ABBREV:
        return _ABBREV[low]
    if low in _KEEP_LOWER:
        return low
    if "-" in tok and not tok.startswith("-") and not tok.endswith("-"):
        return "-".join(_title_token(part, index, total) for part in tok.split("-") if part)
    if _UNITISH.match(tok) or _RATIO.match(tok):
        return tok.lower() if re.search(r"[a-zA-Z]", tok) else tok
    if index not in (0, total - 1) and low in _SMALL:
        return low
    if re.search(r"\d", tok) and re.search(r"[a-zA-Z]", tok):
        # 10ml, 3ml, 10ct — keep compact units lower.
        m = re.match(r"^(\d+(?:\.\d+)?)([a-zA-Z]+)$", tok)
        if m:
            return m.group(1) + m.group(2).lower()
    if tok.isupper() and len(tok) <= 5 and low in _ABBREV:
        return _ABBREV[low]
    return tok[:1].upper() + tok[1:].lower() if tok else tok


def title_case_product_name(name: str) -> str:
    """Buyer Title Case. Keeps dose units and known peptide abbreviations."""
    cleaned = clean_product_name(name) or display_product_name(name)
    cleaned = _fix_typos(cleaned)
    if not cleaned:
        return "Item"
    tokens = _TOKEN.findall(cleaned)
    words = [t for t in tokens if t not in "/()·,;+-"]
    n = len(words)
    titled: list[str] = []
    wi = 0
    for t in tokens:
        if t in "/()·,;+-":
            titled.append(t)
            continue
        titled.append(_title_token(t, wi, n))
        wi += 1
    parts: list[str] = []
    for t in titled:
        if not parts:
            parts.append(t)
            continue
        prev = parts[-1]
        if t in ")]},;":
            parts.append(t)
        elif t in "/·":
            parts.append(f" {t} ")
        elif t in "+-":
            parts.append(t)
        elif t == "(":
            parts.append(" (")
        elif prev[-1:] in "(/":
            parts.append(t)
        else:
            parts.append(" " + t)
    s = "".join(parts)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,);])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"\s*/\s*", " / ", s)
    s = re.sub(r"\s*\+\s*", " + ", s)
    s = re.sub(r"\s*·\s*", " · ", s)
    return s.strip(" -·/") or "Item"


def pretty_name(name: str) -> str:
    """Clean + Title Case + known alias. Does not invent prices."""
    cleaned = clean_product_name(name) or display_product_name(name)
    cleaned = _DISAMBIG_PRICE.sub("", cleaned).strip()
    key = " ".join(cleaned.casefold().split())
    if key in _NAME_ALIASES:
        return _NAME_ALIASES[key]
    for prefix, alias in _NAME_PREFIX_ALIASES:
        if key.startswith(prefix):
            return alias
    return title_case_product_name(cleaned)


def _format_note(hay: str) -> str:
    if _has(hay, "kit"):
        return "Packed as a kit."
    if any(_has(hay, n) for n in ("cream", "serum", "filler", "tret")):
        return "Topical format."
    if any(_has(hay, n) for n in ("tab", "tabs", "capsule", "capsules")):
        return "Tablet / capsule format."
    if any(_has(hay, n) for n in ("ampoule", "ampule")):
        return "Ampoule format."
    if any(
        _has(hay, n)
        for n in ("case", "container", "flexi", "pen", "applicator", "roller")
    ):
        return "Factory gadget."
    if any(_has(hay, n) for n in ("water", "saline", "buffer", "bac")):
        return "Mixer vial."
    if any(_has(hay, n) for n in ("vial", "inj", "injectable", "mg", "ml", "iu")):
        return "Ships as a vial."
    return ""


def item_description(name: str, category: str | None = None) -> str:
    """Short Unicorn voice. Matches the product. No medical claims."""
    pretty = pretty_name(name)
    key = " ".join(clean_product_name(name).casefold().split())
    pretty_key = " ".join(pretty.casefold().split())
    if key in _SPECIAL_DESC:
        return _SPECIAL_DESC[key]
    if pretty_key in _SPECIAL_DESC:
        return _SPECIAL_DESC[pretty_key]
    cat = category or categorize(name)
    inferred = categorize(pretty)
    if cat == "Accessories" and inferred != "Accessories":
        cat = inferred
    hay = _hay(pretty)
    for needle, blurb in _COMPOUND_BLURB:
        if _has(hay, needle):
            return storefront_label(f"{pretty} — {blurb}", 220) or blurb
    tag = CATEGORY_TAG.get(cat) or "Factory sparkle"
    note = _format_note(hay)
    if cat == "Accessories":
        text = f"{pretty} — {tag}."
        if note:
            text += f" {note}"
    else:
        text = f"{pretty} — {tag}."
        if note:
            text += f" {note}"
        text += " Research-shelf only."
    return storefront_label(text, 220) or tag


def image_slug(name: str, category: str | None = None) -> str:
    """Pick a themed art slug that matches the real item type."""
    hay = _hay(name)
    cat = category or categorize(name)
    if any(
        _has(hay, n)
        for n in (
            "pen cart",
            "injection pen",
            "pen noodles",
            "assorted colors",
        )
    ) or hay in {"pen", "pens"}:
        return "pen"
    if any(_has(hay, n) for n in ("capsule", "capsules")):
        return "capsules"
    if any(_has(hay, n) for n in ("tab", "tabs", "blister")):
        return "tabs"
    if any(
        _has(hay, n)
        for n in ("cream", "serum", "filler", "tret", "facial")
    ):
        return "cream"
    if any(_has(hay, n) for n in ("ampoule", "ampule")):
        return "ampoule"
    if any(
        _has(hay, n)
        for n in ("blend", "mix", "stack", "klow", "glow")
    ):
        return "blend"
    if any(
        _has(hay, n)
        for n in ("bac water", "bac saline", "unicorn tears", "aod water")
    ):
        return "bac"
    if any(
        _has(hay, n)
        for n in ("case", "container", "flexi", "spike", "applicator", "roller")
    ):
        return "accessories"
    return CATEGORY_IMAGE.get(cat, "glp1")


def catalog_image_path(slug: str) -> Path | None:
    name = slug if slug.endswith(".jpg") else f"{slug}.jpg"
    if not _SLUG_RE.match(name):
        return None
    path = STATIC_DIR / name
    if path.is_file():
        return path
    return None


def read_catalog_image(filename: str) -> tuple[bytes, str] | None:
    """Safe read of a packaged catalog jpg. None if the name is not a slug."""
    if not _SLUG_RE.match(filename or ""):
        return None
    path = STATIC_DIR / filename
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return data, "image/jpeg"


def public_base_url() -> str:
    for key in ("PANEL_BASE_URL", "RENDER_EXTERNAL_URL"):
        raw = (os.getenv(key) or "").strip().rstrip("/")
        if raw:
            return raw
    return "https://unicornfartzz-bot.onrender.com"


def public_image_url(slug: str) -> str:
    name = slug if slug.endswith(".jpg") else f"{slug}.jpg"
    return f"{public_base_url()}/catalog-img/{name}"


def _ours_photo(value: str | None) -> bool:
    s = str(value or "").strip()
    if not s:
        return True
    return bool(_IMG_URL_RE.search(s))


def public_categories(products: list[dict]) -> list[dict]:
    """Chip list for categories that actually have SKUs on this shelf."""
    seen = {str(p.get("category") or "") for p in products}
    out = []
    for cid, label, tag in CATEGORIES:
        if cid in seen:
            out.append({"id": cid, "label": label, "tag": tag})
    return out


def enrich_public_product(public: dict, row: dict) -> dict:
    """Fill Unicorn buyer fields without changing price/stock."""
    raw_name = str(row.get("name") or public.get("name") or "")
    name = pretty_name(raw_name)
    inferred = categorize(raw_name)
    stored_cat = storefront_label(row.get("category"), 40) or ""
    # Honest shelf wins over a leftover Accessories misfile.
    cat = inferred if (not stored_cat or stored_cat == "Accessories") else stored_cat
    if stored_cat == "Accessories" and inferred != "Accessories":
        cat = inferred
    stored_desc = storefront_label(row.get("description"), 220) or ""
    desc = stored_desc or item_description(raw_name, cat)
    if cat != "Accessories" and re.search(
        r"gadget|hardware|tidier magic", desc, re.I
    ):
        desc = item_description(raw_name, cat)
    photo = str(public.get("photo_url") or "").strip()
    raw_photo = str(row.get("photo_file_id") or "").strip()
    if not photo.lower().startswith("http") and not raw_photo:
        photo = public_image_url(image_slug(raw_name, cat))
    public["name"] = name
    public["category"] = cat
    public["description"] = desc
    public["photo_url"] = photo
    public["tag"] = CATEGORY_TAG.get(cat, "certified magical")
    return public


def _price_label(price: float) -> str:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return ""
    if p <= 0:
        return ""
    if abs(p - round(p)) < 0.001:
        return f"${int(round(p))}"
    return f"${p:.2f}"


def plan_catalog_ux(products: list[dict]) -> list[dict]:
    """Per-row field patches. Never includes price/stock/active."""
    prepped: list[tuple[dict, str, str]] = []
    counts: dict[str, int] = {}
    for p in products:
        if int(p.get("active") or 0) != 1:
            continue
        pretty = pretty_name(str(p.get("name") or ""))
        cat = categorize(pretty or str(p.get("name") or ""))
        prepped.append((p, pretty, cat))
        counts[pretty.casefold()] = counts.get(pretty.casefold(), 0) + 1

    patches: list[dict] = []
    for p, pretty, cat in prepped:
        fields: dict[str, Any] = {}
        display = pretty
        collide = counts.get(pretty.casefold(), 0) > 1
        if collide:
            label = _price_label(p.get("price") or 0)
            if not (p.get("variant_group") or "").strip():
                fields["variant_group"] = pretty[:80]
            if not (p.get("variant_label") or "").strip() and label:
                fields["variant_label"] = label[:80]
        current = " ".join(str(p.get("name") or "").split())
        if display and display != current:
            fields["name"] = display
        stored_cat = storefront_label(p.get("category"), 40) or ""
        if cat != stored_cat:
            fields["category"] = cat
        desc = item_description(str(p.get("name") or pretty), cat)
        stored_desc = storefront_label(p.get("description"), 220) or ""
        if desc and desc != stored_desc:
            fields["description"] = desc
        if _ours_photo(p.get("photo_file_id")):
            url = public_image_url(image_slug(str(p.get("name") or pretty), cat))
            if str(p.get("photo_file_id") or "").strip() != url:
                fields["photo_file_id"] = url
        if fields:
            patches.append({"product_id": int(p["id"]), "fields": fields})
    return patches


def apply_catalog_ux(shop_chat_id: int) -> dict:
    """Persist Unicorn catalog UX on one shop. Never deletes. Never edits prices."""
    import db

    sid = int(shop_chat_id)
    products = db.list_products(sid, active_only=True)
    before_prices = {
        int(p["id"]): (float(p.get("price") or 0), p.get("kit_price"), int(p.get("stock") or 0))
        for p in products
    }
    patches = plan_catalog_ux(products)
    names = cats = descs = photos = variants = 0
    for patch in patches:
        fields = dict(patch["fields"])
        fields.pop("price", None)
        fields.pop("kit_price", None)
        fields.pop("stock", None)
        fields.pop("active", None)
        if not fields:
            continue
        db.update_product(int(patch["product_id"]), **fields)
        if "name" in fields:
            names += 1
        if "category" in fields:
            cats += 1
        if "description" in fields:
            descs += 1
        if "photo_file_id" in fields:
            photos += 1
        if "variant_group" in fields or "variant_label" in fields:
            variants += 1

    after = db.list_products(sid, active_only=True)
    for p in after:
        pid = int(p["id"])
        if pid not in before_prices:
            continue
        old = before_prices[pid]
        if float(p.get("price") or 0) != old[0]:
            raise RuntimeError(f"catalog ux changed price on product {pid}")
        if p.get("kit_price") != old[1]:
            raise RuntimeError(f"catalog ux changed kit_price on product {pid}")
        if int(p.get("stock") or 0) != old[2]:
            raise RuntimeError(f"catalog ux changed stock on product {pid}")

    return {
        "ok": True,
        "shop_chat_id": sid,
        "renames": names,
        "categories": cats,
        "descriptions": descs,
        "photos": photos,
        "variants": variants,
        "products": len(after),
        "msg": (
            f"Catalog UX: {names} name(s), {cats} categor"
            f"{'y' if cats == 1 else 'ies'}, {descs} description(s), "
            f"{photos} photo(s). Prices unchanged."
        ),
    }
