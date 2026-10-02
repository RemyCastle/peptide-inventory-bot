"""Public prices from a Show Me Source (SMS) cost.

SPBC and Jekyll A. Hyde use fixed formulas. Patriotic Peptides and franchisee
shops keep the catalog price already stored on the product. This module does
not write those rows. Franchisee hidden service fees stay on shipping
(`franchise.customer_shipping_total`): base shipping plus the hidden fee.
"""

from __future__ import annotations

SPBC_VIAL_ADDON = 35.0
SPBC_KIT_ADDON = 250.0
JEKYLL_MULTIPLIER = 2.0

_SPBC = {"spbc", "springfield", "springfield pbc"}
_JEKYLL = {"jekyll", "jekyll a. hyde", "jekyll_a_hyde", "hyde"}


def _channel(name: str) -> str:
    return " ".join(str(name or "").strip().lower().replace("_", " ").split())


def _money(value: float) -> float:
    return round(float(value) + 0.0, 2)


def public_price(
    channel: str,
    sms_cost: float,
    *,
    kind: str = "vial",
    listed_price: float | None = None,
) -> float:
    """Buyer-facing unit price for one vial/single or one kit.

    `kind` is `vial` (or `single`) or `kit`. `sms_cost` is the Show Me Source
    cost for that same unit (one vial, or one kit). `listed_price` is the
    price already stored for a Patriotic or franchisee product.
    """
    ch = _channel(channel)
    unit = str(kind or "vial").strip().lower()
    if unit == "single":
        unit = "vial"
    if unit not in {"vial", "kit"}:
        raise ValueError("kind must be vial or kit")
    cost = float(sms_cost)
    if cost < 0:
        raise ValueError("sms_cost cannot be negative")

    if ch in _SPBC:
        addon = SPBC_KIT_ADDON if unit == "kit" else SPBC_VIAL_ADDON
        return _money(cost + addon)
    if ch in _JEKYLL:
        return _money(cost * JEKYLL_MULTIPLIER)
    if "patriot" in ch or "franchise" in ch:
        if listed_price is None:
            return _money(cost)
        return _money(float(listed_price))
    raise ValueError(f"unknown pricing channel: {channel}")


def franchisee_customer_shipping(base_shipping: float, hidden_service_fee: float) -> float:
    """Customer shipping from current franchise code: base plus hidden fee.

    The hidden fee is still added when base shipping is 0. It is not a
    percent markup on the vial or kit price.
    """
    hidden = float(hidden_service_fee)
    if hidden < 0:
        hidden = 0.0
    return _money(float(base_shipping) + hidden)
