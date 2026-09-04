"""Gate 4 — Budget algebra.

All arithmetic is integer paise inside a transaction that also performs the
reservation later in the pipeline, so check and use cannot drift.

Edge cases handled here:
  * Per-transaction cap AND cumulative cap, checked against
    `committed + reserved + this_amount`. Ignoring `reserved` is the classic
    concurrency hole: two parallel proposals each pass, together they overspend.
  * Zero amount — a ₹0 order is a probe, not a purchase.
  * Currency mismatch between action, cart and mandate.
  * Refunds (`COMPENSATION`) never consume budget; they restore it. They are
    exempted here and accounted by the executor.
  * `capture_payment` does not add new spend — the money was already reserved
    when the order was authorised — so it is checked against the per-txn cap
    only, never re-added to the cumulative total.
  * Overflow: `money.add` re-validates every sum against MAX_PAISE.
"""
from __future__ import annotations

from ..errors import Reason
from ..models import ActionKind, AttemptClass
from ..money import MoneyError, add
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "budget", 4

_NEW_SPEND = {ActionKind.CREATE_ORDER, ActionKind.CREATE_PAYMENT_LINK}


@timed
def gate(ctx: GateContext) -> GateResult:
    assert ctx.action and ctx.intent
    a, c = ctx.action, ctx.intent.constraints

    if a.attempt_class is AttemptClass.COMPENSATION:
        return ok(NAME, ORDINAL, "refund exempt from budget consumption", exempt=True)

    if a.amount_paise <= 0:
        return deny(NAME, ORDINAL, Reason.BUDGET_ZERO_AMOUNT, "amount must be greater than zero")

    if ctx.cart is not None and ctx.cart.currency != ctx.intent.currency:
        return deny(NAME, ORDINAL, Reason.BUDGET_CURRENCY_MISMATCH,
                    f"cart {ctx.cart.currency} vs mandate {ctx.intent.currency}")

    if a.amount_paise > c.max_per_txn_paise:
        return deny(NAME, ORDINAL, Reason.BUDGET_PER_TXN_EXCEEDED,
                    f"{a.amount_paise} exceeds per-transaction cap {c.max_per_txn_paise}",
                    amount_paise=a.amount_paise, cap_paise=c.max_per_txn_paise)

    state = ctx.store.spend_state(ctx.intent.mandate_id)
    if a.action not in _NEW_SPEND:
        return ok(NAME, ORDINAL, f"{a.action} consumes no new budget",
                  committed=state["committed"], reserved=state["reserved"])

    try:
        projected = add(state["committed"], state["reserved"], a.amount_paise)
    except MoneyError as e:
        return deny(NAME, ORDINAL, Reason.BUDGET_TOTAL_EXCEEDED, f"arithmetic guard: {e}")

    if projected > c.max_total_paise:
        return deny(NAME, ORDINAL, Reason.BUDGET_TOTAL_EXCEEDED,
                    f"committed {state['committed']} + reserved {state['reserved']} + {a.amount_paise}"
                    f" = {projected} exceeds mandate total {c.max_total_paise}",
                    projected_paise=projected, max_total_paise=c.max_total_paise,
                    headroom_paise=max(c.max_total_paise - state["committed"] - state["reserved"], 0))

    ctx.scratch["reserve_paise"] = a.amount_paise
    return ok(NAME, ORDINAL, "within per-transaction and cumulative caps",
              projected_paise=projected, max_total_paise=c.max_total_paise,
              headroom_paise=c.max_total_paise - projected)
