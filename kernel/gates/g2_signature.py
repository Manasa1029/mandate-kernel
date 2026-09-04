"""Gate 2 — Signature and authority.

Three separate questions, all of which a naive implementation conflates:
  1. Is the signature cryptographically valid?      (crypto)
  2. Is the signer the right *kind* of party?        (role)
  3. Is the signer authorised for this subject?      (delegation)

Edge cases handled here:
  * Unknown key_id — no implicit trust-on-first-use.
  * Revoked key — a user who killed their agent's key must be obeyed instantly.
  * Role confusion — a merchant key signing an intent mandate, or an agent key
    signing a cart mandate (self-quoting attack) both fail.
  * Delegation — the agent that signed the action must appear in the intent's
    `delegated_agents`. A valid signature from an unlisted agent is worthless.
  * Cart binding — if the cart declares an `intent_ref`, it must be *this*
    intent, otherwise a cart quoted for a different (larger) mandate could be
    replayed under this one.
  * Envelope stripping — we verify over the payload only, so an attacker cannot
    change what was authorised by rewriting the wrapper.
"""
from __future__ import annotations

from ..crypto import KeyRole, VerifyResult, verify_envelope
from ..errors import Reason
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "signature", 2

_MAP = {
    VerifyResult.UNKNOWN_KEY: Reason.SIG_UNKNOWN_KEY,
    VerifyResult.BAD_ALG: Reason.SIG_BAD_ALG,
    VerifyResult.REVOKED: Reason.SIG_UNKNOWN_KEY,
    VerifyResult.INVALID: Reason.SIG_INVALID,
    VerifyResult.MALFORMED: Reason.SIG_INVALID,
}


@timed
def gate(ctx: GateContext) -> GateResult:
    assert ctx.action and ctx.intent

    res, user_rec = verify_envelope(ctx.registry, ctx.request.intent.model_dump(), KeyRole.USER)
    if res is not VerifyResult.OK:
        return deny(NAME, ORDINAL, _MAP[res], f"intent mandate signature: {res}")
    ctx.user_key = user_rec
    if user_rec.subject != ctx.intent.subject:
        return deny(NAME, ORDINAL, Reason.SIG_SUBJECT_MISMATCH,
                    f"key belongs to {user_rec.subject!r}, mandate claims {ctx.intent.subject!r}")

    res, agent_rec = verify_envelope(ctx.registry, ctx.request.action.model_dump(), KeyRole.AGENT)
    if res is not VerifyResult.OK:
        return deny(NAME, ORDINAL, _MAP[res], f"proposed action signature: {res}")
    ctx.agent_key = agent_rec
    if agent_rec.key_id not in ctx.intent.delegated_agents:
        return deny(NAME, ORDINAL, Reason.SIG_AGENT_NOT_DELEGATED,
                    f"agent {agent_rec.key_id} is not delegated by this mandate")

    if ctx.cart is not None:
        assert ctx.request.cart is not None
        res, merch_rec = verify_envelope(ctx.registry, ctx.request.cart.model_dump(), KeyRole.MERCHANT)
        if res is not VerifyResult.OK:
            return deny(NAME, ORDINAL, _MAP[res], f"cart mandate signature: {res}")
        ctx.merchant_key = merch_rec
        if merch_rec.subject != ctx.cart.merchant_id:
            return deny(NAME, ORDINAL, Reason.SIG_MERCHANT_KEY_MISMATCH,
                        f"cart signed by {merch_rec.subject!r} but claims merchant {ctx.cart.merchant_id!r}")
        if ctx.cart.intent_ref is not None and ctx.cart.intent_ref != ctx.intent.mandate_id:
            return deny(NAME, ORDINAL, Reason.SIG_CART_NOT_BOUND_TO_INTENT,
                        "cart was quoted against a different intent mandate")

    return ok(NAME, ORDINAL, "all signatures valid and delegated",
              user_key=ctx.user_key.key_id, agent_key=ctx.agent_key.key_id,
              merchant_key=ctx.merchant_key.key_id if ctx.merchant_key else None)
