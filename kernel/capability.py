"""Capability tokens — the only thing the execution layer accepts.

A capability is deliberately *not* a JWT the agent can inspect and re-use. It is
an opaque 256-bit random handle to a server-side record that is:

    single-use    (burned atomically in SQL)
    single-amount (exact paise, no ranges)
    single-payee  (exact normalised payee)
    single-verb   (create_order cannot be redeemed as create_refund)
    short-lived   (90s default — long enough for a provider call, not a nap)

Edge cases handled here:
  * Burn-before-call. We mark the token spent *before* the provider request, so a
    crash mid-flight cannot be retried into a double charge; recovery goes
    through the idempotency record instead.
  * Scope re-verification at redemption. Even with a valid token, the executor
    re-checks amount/payee/verb against the request it is about to make. Defence
    in depth against a bug in the calling layer.
"""
from __future__ import annotations

import secrets

from .config import KernelConfig
from .errors import Reason
from .models import Capability, ProposedAction, now_s
from .store import Store


def mint(store: Store, cfg: KernelConfig, action: ProposedAction, mandate_id: str,
         idempotency_key: str) -> Capability:
    token = "cap_" + secrets.token_urlsafe(32)
    cap = Capability(
        token=token,
        action_id=action.action_id,
        mandate_id=mandate_id,
        idempotency_key=idempotency_key,
        action=action.action,
        amount_paise=action.amount_paise,
        merchant_id=action.merchant_id,
        payee=action.payee,
        reference_id=action.reference_id,
        issued_at=now_s(),
        expires_at=now_s() + cfg.capability_ttl_s,
    )
    store.capability_put(token, cap.model_dump(mode="json"), cap.expires_at)
    return cap


class CapabilityError(Exception):
    def __init__(self, reason: Reason, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def redeem(store: Store, token: str, *, expect_amount: int, expect_payee: str,
           expect_action: str) -> Capability:
    """Burn the token and confirm it authorises exactly this call."""
    ok, payload, why = store.capability_spend(token)
    if not ok:
        reason = {
            "expired": Reason.EXEC_CAPABILITY_EXPIRED,
            "already_spent": Reason.EXEC_CAPABILITY_SPENT,
            "unknown_capability": Reason.EXEC_CAPABILITY_SCOPE,
        }[why]
        raise CapabilityError(reason, why)
    cap = Capability.model_validate(payload)
    if cap.amount_paise != expect_amount:
        raise CapabilityError(Reason.EXEC_CAPABILITY_SCOPE,
                              f"amount {expect_amount} != authorised {cap.amount_paise}")
    if cap.payee != expect_payee:
        raise CapabilityError(Reason.EXEC_CAPABILITY_SCOPE, "payee outside capability scope")
    if str(cap.action) != expect_action:
        raise CapabilityError(Reason.EXEC_CAPABILITY_SCOPE,
                              f"verb {expect_action} != authorised {cap.action}")
    return cap
