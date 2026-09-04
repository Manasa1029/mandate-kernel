"""Gate 1 — Schema.

Rejects anything that is not exactly the shape we signed for. This is where an
LLM's creativity dies quietly: unknown fields, string amounts, floats, negative
quantities, unsupported action verbs, non-INR currency, absurd magnitudes.

Edge cases handled here:
  * `extra="forbid"` — a hallucinated `"discount_override": true` is fatal.
  * Strict ints — `"amount_paise": "400000"` is fatal (string coercion is how
    "4000.00" silently becomes 4000 paise instead of 400000).
  * Floats — blocked twice: pydantic StrictInt, and canonical.py at sign time.
  * A cart is mandatory for order/link actions and forbidden for capture/refund
    (capture references an existing payment; re-supplying a cart invites a
    mismatched-price attack).
"""
from __future__ import annotations

from pydantic import ValidationError

from ..errors import Reason
from ..models import ActionKind, CartMandate, IntentMandate, ProposedAction
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "schema", 1

_CART_REQUIRED = {ActionKind.CREATE_ORDER, ActionKind.CREATE_PAYMENT_LINK}
_CART_FORBIDDEN = {ActionKind.CAPTURE_PAYMENT, ActionKind.CREATE_REFUND}


def _fmt(err: ValidationError) -> str:
    first = err.errors()[0]
    loc = ".".join(str(p) for p in first["loc"]) or "<root>"
    return f"{loc}: {first['msg']}"


@timed
def gate(ctx: GateContext) -> GateResult:
    try:
        ctx.action = ProposedAction.model_validate(ctx.request.action.payload)
    except ValidationError as e:
        reason = Reason.SCHEMA_UNKNOWN_FIELD if "extra_forbidden" in str(e) else Reason.SCHEMA_INVALID
        return deny(NAME, ORDINAL, reason, f"action {_fmt(e)}")

    try:
        ctx.intent = IntentMandate.model_validate(ctx.request.intent.payload)
    except ValidationError as e:
        return deny(NAME, ORDINAL, Reason.SCHEMA_INVALID, f"intent {_fmt(e)}")

    action = ctx.action
    if action.currency != "INR" or ctx.intent.currency != "INR":
        return deny(NAME, ORDINAL, Reason.SCHEMA_CURRENCY, "only INR is supported")

    if action.action in _CART_REQUIRED and ctx.request.cart is None:
        return deny(NAME, ORDINAL, Reason.SCHEMA_INVALID, f"{action.action} requires a cart mandate")
    if action.action in _CART_FORBIDDEN and ctx.request.cart is not None:
        return deny(NAME, ORDINAL, Reason.SCHEMA_INVALID,
                    f"{action.action} must not carry a cart mandate")

    if ctx.request.cart is not None:
        try:
            ctx.cart = CartMandate.model_validate(ctx.request.cart.payload)
        except ValidationError as e:
            return deny(NAME, ORDINAL, Reason.SCHEMA_INVALID, f"cart {_fmt(e)}")
        if ctx.cart.currency != "INR":
            return deny(NAME, ORDINAL, Reason.SCHEMA_CURRENCY, "cart currency must be INR")
        if ctx.cart.cart_id != action.cart_ref:
            return deny(NAME, ORDINAL, Reason.SCHEMA_INVALID, "action.cart_ref does not name this cart")

    if ctx.intent.mandate_id != action.intent_ref:
        return deny(NAME, ORDINAL, Reason.SCHEMA_INVALID, "action.intent_ref does not name this mandate")

    return ok(NAME, ORDINAL, f"{action.action} well-formed", action=str(action.action),
              amount_paise=action.amount_paise)
