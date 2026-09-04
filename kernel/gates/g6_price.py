"""Gate 6 — Price binding. Zero tolerance, recomputed from first principles.

The kernel does not trust any total it is handed — not the agent's, not even the
merchant's. It recomputes the cart from line items and compares three numbers
that must all agree exactly:

    sum(qty * unit_price + tax) + shipping  ==  cart.total_paise  ==  action.amount_paise

Edge cases handled here:
  * Line math — a merchant (or a compromised quote service) that signs
    `total_paise` inconsistent with its own line items is rejected. This is
    what stops "signed, therefore correct".
  * Subtotal/tax fields are recomputed independently, so a cart that balances
    only because tax absorbs an error still fails.
  * Cart hash binding — `action.cart_hash` must equal the canonical digest of
    the signed cart payload. Without this, an agent could present cart A's hash
    with cart B's contents in a downstream system that trusts the hash.
  * Action amount must equal the cart total exactly. No partial payments in v1;
    allowing them without an explicit mandate clause is how "pay 1% now" becomes
    "pay 100% later, unbounded".
  * Refund amount must not exceed the referenced cart total (checked by the
    executor against the actual captured amount too).
"""
from __future__ import annotations

from ..canonical import digest
from ..errors import Reason
from ..models import ActionKind, AttemptClass
from ..money import MoneyError, add, mul
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "price_binding", 6


@timed
def gate(ctx: GateContext) -> GateResult:
    assert ctx.action and ctx.intent

    if ctx.cart is None:
        # Capture and refund carry no cart (gate 1 forbids one), so there is no line
        # math to recompute. That does NOT mean the amount is unbounded: the first
        # version of this gate returned ok() here, which made an arbitrarily large
        # refund pass all eight gates. The ledger is the ceiling instead.
        a = ctx.action
        state = ctx.store.spend_state(a.intent_ref)
        settled = state["committed"]
        authorised = settled + state["reserved"]

        if a.attempt_class is AttemptClass.COMPENSATION:
            if settled <= 0:
                return deny(NAME, ORDINAL, Reason.PRICE_NO_SETTLED_PAYMENT,
                            "refund requested against a mandate with no settled spend",
                            settled_paise=settled)
            if a.amount_paise > settled:
                return deny(NAME, ORDINAL, Reason.PRICE_REFUND_EXCEEDS_SETTLED,
                            f"refund {a.amount_paise} exceeds settled {settled}",
                            refund_paise=a.amount_paise, settled_paise=settled)
            return ok(NAME, ORDINAL, "refund bounded by settled spend on this mandate",
                      refund_paise=a.amount_paise, settled_paise=settled)

        if a.amount_paise > authorised:
            return deny(NAME, ORDINAL, Reason.PRICE_CAPTURE_EXCEEDS_AUTHORISED,
                        f"capture {a.amount_paise} exceeds authorised {authorised}",
                        capture_paise=a.amount_paise, authorised_paise=authorised)
        return ok(NAME, ORDINAL, "capture bounded by authorised spend on this mandate",
                  capture_paise=a.amount_paise, authorised_paise=authorised)

    cart, a = ctx.cart, ctx.action

    try:
        subtotal = 0
        tax = 0
        for item in cart.items:
            if item.qty <= 0:
                return deny(NAME, ORDINAL, Reason.PRICE_QUANTITY_INVALID, f"{item.sku} qty {item.qty}")
            subtotal = add(subtotal, mul(item.unit_price_paise, item.qty))
            tax = add(tax, item.tax_paise)
        recomputed_total = add(subtotal, tax, cart.shipping_paise)
    except MoneyError as e:
        return deny(NAME, ORDINAL, Reason.PRICE_LINE_MATH, f"arithmetic guard: {e}")

    if subtotal != cart.subtotal_paise:
        return deny(NAME, ORDINAL, Reason.PRICE_LINE_MATH,
                    f"recomputed subtotal {subtotal} != declared {cart.subtotal_paise}",
                    recomputed=subtotal, declared=cart.subtotal_paise)
    if tax != cart.tax_paise:
        return deny(NAME, ORDINAL, Reason.PRICE_LINE_MATH,
                    f"recomputed tax {tax} != declared {cart.tax_paise}",
                    recomputed=tax, declared=cart.tax_paise)
    if recomputed_total != cart.total_paise:
        return deny(NAME, ORDINAL, Reason.PRICE_CART_TOTAL,
                    f"recomputed total {recomputed_total} != declared {cart.total_paise}",
                    recomputed=recomputed_total, declared=cart.total_paise)

    computed_hash = digest(ctx.request.cart.payload)  # type: ignore[union-attr]
    ctx.computed_cart_hash = computed_hash
    if a.cart_hash != computed_hash:
        return deny(NAME, ORDINAL, Reason.PRICE_CART_HASH,
                    "action.cart_hash does not match the signed cart payload",
                    claimed=a.cart_hash, computed=computed_hash)

    if a.action in (ActionKind.CREATE_ORDER, ActionKind.CREATE_PAYMENT_LINK) and a.amount_paise != cart.total_paise:
        return deny(NAME, ORDINAL, Reason.PRICE_ACTION_AMOUNT,
                    f"action amount {a.amount_paise} != signed cart total {cart.total_paise}",
                    action_amount=a.amount_paise, cart_total=cart.total_paise)

    # Defence in depth: unreachable today because gate 1 forbids a cart on refunds,
    # kept so that relaxing gate 1 later cannot silently unbound refunds.
    if a.attempt_class is AttemptClass.COMPENSATION and a.amount_paise > cart.total_paise:
        return deny(NAME, ORDINAL, Reason.PRICE_ACTION_AMOUNT, "refund exceeds cart total")

    return ok(NAME, ORDINAL, "line math, cart total, cart hash and action amount all agree",
              total_paise=cart.total_paise, cart_hash=computed_hash[:16])
