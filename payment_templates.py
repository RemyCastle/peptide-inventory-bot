"""Shared payment method templates for wizard + admin panel."""

from __future__ import annotations

from typing import Any


METHOD_TYPES = ("cashapp", "venmo", "crypto", "zelle", "custom")


def normalize_cashtag(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.startswith("$"):
        s = "$" + s.lstrip("$")
    return s


def normalize_venmo(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.startswith("@"):
        s = "@" + s.lstrip("@")
    return s


def render_cashapp(cashtag: str) -> dict[str, Any]:
    tag = normalize_cashtag(cashtag)
    name = "Cash App"
    instructions = (
        f"💸 *Cash App*\n"
        f"Send payment to: `{tag}`\n"
        f"Include your order number in the payment note."
    )
    return {
        "name": name,
        "instructions": instructions,
        "method_type": "cashapp",
        "cashtag": tag,
        "handle": None,
        "chain": None,
        "address": None,
        "network_note": None,
    }


def render_venmo(handle: str) -> dict[str, Any]:
    h = normalize_venmo(handle)
    name = "Venmo"
    instructions = (
        f"💙 *Venmo*\n"
        f"Send payment to: `{h}`\n"
        f"Include your order number in the payment note."
    )
    return {
        "name": name,
        "instructions": instructions,
        "method_type": "venmo",
        "cashtag": None,
        "handle": h,
        "chain": None,
        "address": None,
        "network_note": None,
    }


def render_crypto(coin: str, address: str, network_note: str = "") -> dict[str, Any]:
    coin_s = (coin or "Crypto").strip().upper()
    addr = (address or "").strip()
    note = (network_note or "").strip()
    name = f"Crypto ({coin_s})" if coin_s else "Crypto"
    lines = [
        f"₿ *{name}*",
        f"Send to: `{addr}`",
    ]
    if note:
        lines.append(f"Network: {note}")
    lines.append(
        "⚠️ Double-check the network before sending — wrong network = lost funds."
    )
    lines.append("Include your order number in the memo if the network supports it.")
    return {
        "name": name,
        "instructions": "\n".join(lines),
        "method_type": "crypto",
        "cashtag": None,
        "handle": None,
        "chain": coin_s,
        "address": addr,
        "network_note": note or None,
    }


def render_zelle(contact: str) -> dict[str, Any]:
    c = (contact or "").strip()
    name = "Zelle"
    instructions = (
        f"🏦 *Zelle*\n"
        f"Send to: `{c}`\n"
        f"Include your order number in the payment note."
    )
    return {
        "name": name,
        "instructions": instructions,
        "method_type": "zelle",
        "cashtag": None,
        "handle": c,
        "chain": None,
        "address": None,
        "network_note": None,
    }


def render_custom(instructions: str, name: str = "Custom") -> dict[str, Any]:
    text = (instructions or "").strip()
    n = (name or "Custom").strip() or "Custom"
    return {
        "name": n,
        "instructions": text,
        "method_type": "custom",
        "cashtag": None,
        "handle": None,
        "chain": None,
        "address": None,
        "network_note": None,
    }


def template_prompts(method_type: str) -> list[str]:
    """Ordered prompts for collecting structured fields."""
    mt = (method_type or "").lower()
    if mt == "cashapp":
        return ["What's your $Cashtag?"]
    if mt == "venmo":
        return ["What's your Venmo @handle?"]
    if mt == "crypto":
        return [
            "Which coin? (BTC / ETH / USDT / Other)",
            "What's the wallet address?",
            'Any network note? (e.g. "USDT — TRC20 only") — or send `-` to skip',
        ]
    if mt == "zelle":
        return ["What's your Zelle email or phone number?"]
    if mt == "custom":
        return ["Enter your custom payment instructions:"]
    return ["Enter payment instructions:"]


def render_from_answers(method_type: str, answers: list[str]) -> dict[str, Any]:
    mt = (method_type or "custom").lower()
    answers = [(a or "").strip() for a in answers]
    if mt == "cashapp":
        return render_cashapp(answers[0] if answers else "")
    if mt == "venmo":
        return render_venmo(answers[0] if answers else "")
    if mt == "crypto":
        coin = answers[0] if len(answers) > 0 else "CRYPTO"
        addr = answers[1] if len(answers) > 1 else ""
        note = answers[2] if len(answers) > 2 else ""
        if note == "-":
            note = ""
        return render_crypto(coin, addr, note)
    if mt == "zelle":
        return render_zelle(answers[0] if answers else "")
    return render_custom(answers[0] if answers else "")
