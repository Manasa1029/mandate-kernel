"""Gate 8 — Idempotency. The last gate, because it must claim a lock only once
everything else has already agreed the action is legal.

The key is *derived*, never client-supplied. A client-chosen idempotency key is
a client-chosen double-charge.

    order / link : hash(mandate, verb, cart_hash, escalation_epoch)
    capture      : hash(mandate, verb, reference_id, amount)
    refund       : hash(mandate, verb, reference_id, amount)

Edge cases handled here:
  * RETRY reuses the same key as INITIAL (same instrument, same money) so a
    network-timeout retry cannot double charge.
  * ESCALATION deliberately changes the key via `escalation_epoch = attempt`,
    because switching UPI -> card is a genuinely different payment attempt. The
    budget was already reserved once, so the executor releases the old
    reservation before the new attempt.
  * In-flight collision returns IDEM_IN_FLIGHT rather than executing — two
    workers racing the same cart is normal under a queue redelivery.
  * A completed key returns the stored result verbatim as `replayed_result`.
    The caller sees ALLOW-with-replay semantics: no new provider call, no new
    money, and the original provider ids.
  * A stale `in_flight` row (crashed worker) is reclaimed after a timeout, and
    the reclaim is written to the ledger by the pipeline.
"""
from __future__ import annotations

from ..canonical import digest
from ..errors import Reason
from ..models import ActionKind, AttemptClass
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "idempotency", 8


def derive_key(*, mandate_id: str, action: ActionKind, cart_hash: str | None,
               reference_id: str | None, amount_paise: int, attempt_class: AttemptClass,
               attempt: int) -> str:
    epoch = attempt if attempt_class is AttemptClass.ESCALATION else 0
    if action in (ActionKind.CREATE_ORDER, ActionKind.CREATE_PAYMENT_LINK):
        material = {"m": mandate_id, "v": str(action), "c": cart_hash, "e": epoch}
    else:
        material = {"m": mandate_id, "v": str(action), "r": reference_id, "a": amount_paise}
    return "idem_" + digest(material)[:32]


@timed
def gate(ctx: GateContext) -> GateResult:
    assert ctx.action and ctx.intent
    a = ctx.action

    key = derive_key(
        mandate_id=ctx.intent.mandate_id,
        action=a.action,
        cart_hash=ctx.computed_cart_hash,
        reference_id=a.reference_id,
        amount_paise=a.amount_paise,
        attempt_class=a.attempt_class,
        attempt=a.attempt,
    )
    ctx.idempotency_key = key

    claimed, existing = ctx.store.idem_claim(key, a.action_id, ctx.intent.mandate_id)

    if claimed:
        ctx.claimed_idem = True
        if existing is not None:
            ctx.scratch["idem_reclaimed_from"] = existing["action_id"]
        return ok(NAME, ORDINAL, "idempotency key claimed", idempotency_key=key,
                  reclaimed=existing is not None)

    assert existing is not None
    if existing["state"] == "in_flight":
        return deny(NAME, ORDINAL, Reason.IDEM_IN_FLIGHT,
                    f"another attempt for this key is executing (action {existing['action_id']})",
                    idempotency_key=key)

    ctx.replayed_result = existing["result"]
    return deny(NAME, ORDINAL, Reason.IDEM_REPLAYED,
                f"key already {existing['state']}; returning the original result without re-charging",
                idempotency_key=key, original_action_id=existing["action_id"],
                original_state=existing["state"])
