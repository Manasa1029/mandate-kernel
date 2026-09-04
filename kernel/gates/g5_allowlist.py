"""Gate 5 — Allowlists (merchant, payee, SKU, category) and denylists.

Mirrors UPI's payee-allowlist model: an agent may only pay parties the user
named, and may only buy things the user scoped.

Edge cases handled here:
  * Payee normalisation before comparison. `Store@Paytm` and `store@paytm` are
    the same VPA; comparing raw strings lets an attacker bypass an allowlist
    with a capital letter. Whitespace and zero-width characters are stripped.
  * Homoglyph/unicode confusables are *not* silently normalised — they are
    rejected, because "normalising" an attacker-supplied lookalike into a
    trusted value is itself the vulnerability.
  * Denylist beats allowlist, always, and produces its own reason code.
  * Merchant identity is taken from the *signed cart*, never from the action,
    for cart-bearing verbs; the action's claim must agree.
  * SKU allowlist OR category allowlist may authorise a line; an empty pair was
    already rejected at mandate construction.
  * Capture and refund inherit their merchant/payee from the original reference,
    so only the payee check applies.
"""
from __future__ import annotations

import unicodedata

from ..errors import Reason
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "allowlist", 5

_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"}


def normalise_payee(raw: str) -> tuple[str, bool]:
    """Return (normalised, suspicious). Suspicious means non-ASCII confusable risk."""
    stripped = "".join(ch for ch in raw if ch not in _ZERO_WIDTH).strip()
    nfkc = unicodedata.normalize("NFKC", stripped)
    suspicious = any(ord(ch) > 127 for ch in nfkc)
    return nfkc.casefold(), suspicious


@timed
def gate(ctx: GateContext) -> GateResult:
    assert ctx.action and ctx.intent
    a, c = ctx.action, ctx.intent.constraints

    payee, suspicious = normalise_payee(a.payee)
    if suspicious:
        return deny(NAME, ORDINAL, Reason.ALLOW_PAYEE,
                    "payee contains non-ASCII characters; refusing to normalise a possible confusable",
                    payee=a.payee)

    allowed_payees = {normalise_payee(p)[0] for p in c.allowed_payees}
    denied_payees = {normalise_payee(p)[0] for p in c.denied_payees}

    if payee in denied_payees:
        return deny(NAME, ORDINAL, Reason.ALLOW_DENYLIST_HIT, f"payee {a.payee!r} is denylisted")
    if payee not in allowed_payees:
        return deny(NAME, ORDINAL, Reason.ALLOW_PAYEE,
                    f"payee {a.payee!r} is not in the mandate's payee allowlist",
                    payee=a.payee, allowed=sorted(allowed_payees))

    merchant = ctx.cart.merchant_id if ctx.cart else a.merchant_id
    if ctx.cart is not None and ctx.cart.merchant_id != a.merchant_id:
        return deny(NAME, ORDINAL, Reason.ALLOW_MERCHANT,
                    f"action claims merchant {a.merchant_id!r} but signed cart says {ctx.cart.merchant_id!r}")
    if merchant not in c.allowed_merchants:
        return deny(NAME, ORDINAL, Reason.ALLOW_MERCHANT,
                    f"merchant {merchant!r} is not in the mandate's merchant allowlist",
                    merchant=merchant, allowed=list(c.allowed_merchants))

    if ctx.cart is not None:
        if ctx.cart.payee != a.payee:
            return deny(NAME, ORDINAL, Reason.ALLOW_PAYEE,
                        "action payee does not match the signed cart payee")
        denied_skus = set(c.denied_skus)
        for item in ctx.cart.items:
            if item.sku in denied_skus:
                return deny(NAME, ORDINAL, Reason.ALLOW_DENYLIST_HIT,
                            f"SKU {item.sku!r} is denylisted", sku=item.sku)
            if item.sku in c.allowed_skus:
                continue
            if item.category in c.allowed_categories:
                continue
            if c.allowed_skus and not c.allowed_categories:
                return deny(NAME, ORDINAL, Reason.ALLOW_SKU,
                            f"SKU {item.sku!r} is outside the mandate's SKU allowlist", sku=item.sku)
            return deny(NAME, ORDINAL, Reason.ALLOW_CATEGORY,
                        f"SKU {item.sku!r} (category {item.category!r}) is outside the mandate's scope",
                        sku=item.sku, category=item.category,
                        allowed_categories=list(c.allowed_categories))

    return ok(NAME, ORDINAL, "merchant, payee and every line item are in scope",
              merchant=merchant, payee=payee,
              lines=len(ctx.cart.items) if ctx.cart else 0)
