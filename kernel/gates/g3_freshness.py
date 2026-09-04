"""Gate 3 — Freshness, revocation and replay.

Edge cases handled here:
  * Intent expiry with a bounded clock-skew tolerance. Skew is allowed on
    *inbound* timestamps only; expiry itself is never extended by skew, or an
    attacker with a fast clock gets free time.
  * `issued_at` in the future beyond skew — a mandate minted for later use is a
    pre-dated cheque.
  * Quote expiry (`price_valid_until`) is checked separately from intent expiry
    and produces a distinct reason code, because the remediation differs: the
    agent must re-quote, not re-consent.
  * User revocation — a revoked mandate dies even mid-saga. Compensation
    refunds are explicitly exempt, because refusing to refund after revocation
    would trap the user's money.
  * Nonce replay, scoped per mandate and per surface. Check-and-set is atomic in
    the store, so two concurrent identical requests cannot both pass.
  * Retries legitimately reuse a nonce for the *same* attempt — so the nonce
    scope includes the attempt number, and true double-spend protection is
    Gate 8's job, not this gate's.
"""
from __future__ import annotations

from ..errors import Reason
from ..models import ActionKind, AttemptClass
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "freshness", 3


@timed
def gate(ctx: GateContext) -> GateResult:
    assert ctx.action and ctx.intent
    now, skew = ctx.now, ctx.cfg.clock_skew_s

    state = ctx.store.spend_state(ctx.intent.mandate_id)
    if state["revoked"] and ctx.action.attempt_class is not AttemptClass.COMPENSATION:
        return deny(NAME, ORDINAL, Reason.FRESH_MANDATE_REVOKED, "mandate revoked by user")

    if ctx.intent.issued_at > now + skew:
        return deny(NAME, ORDINAL, Reason.FRESH_ISSUED_IN_FUTURE,
                    f"intent issued_at {ctx.intent.issued_at} is ahead of now {now}")

    if ctx.intent.expires_at < now:
        return deny(NAME, ORDINAL, Reason.FRESH_INTENT_EXPIRED,
                    f"intent expired {now - ctx.intent.expires_at}s ago",
                    expires_at=ctx.intent.expires_at, now=now)

    if ctx.cart is not None:
        if ctx.cart.quoted_at > now + skew:
            return deny(NAME, ORDINAL, Reason.FRESH_ISSUED_IN_FUTURE, "cart quoted in the future")
        if ctx.cart.price_valid_until < now:
            return deny(NAME, ORDINAL, Reason.FRESH_QUOTE_EXPIRED,
                        f"price lock expired {now - ctx.cart.price_valid_until}s ago — re-quote required",
                        price_valid_until=ctx.cart.price_valid_until, now=now)

    # Nonce scope: mandate + action verb + cart + attempt. Deliberately NOT the
    # whole action, or a tampered amount would look like a fresh request.
    scope = f"{ctx.intent.mandate_id}|{ctx.action.action}|{ctx.action.cart_ref}|{ctx.action.attempt}"
    if ctx.store.nonce_seen(scope, ctx.action.client_nonce, ctx.cfg.nonce_ttl_s):
        return deny(NAME, ORDINAL, Reason.FRESH_NONCE_REPLAY,
                    "client_nonce already seen for this mandate/verb/cart/attempt", scope=scope)

    ttl_left = ctx.intent.expires_at - now
    quote_left = (ctx.cart.price_valid_until - now) if ctx.cart else None
    return ok(NAME, ORDINAL, "within all validity windows",
              intent_ttl_s=ttl_left, quote_ttl_s=quote_left)
