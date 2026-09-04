"""Gate contract.

A gate is a pure-ish function `(ctx) -> GateResult`. Rules:

  * Gates never raise for business reasons — they return DENY with a Reason.
    An exception escaping a gate is a bug and is converted to a hard DENY with
    SCHEMA_INVALID by the pipeline, because failing closed is the only safe
    default in a payment path.
  * Gates may write to the store only for check-and-set semantics that must be
    atomic (nonce, idempotency claim). Everything else is read-only.
  * Gates may enrich `ctx`, and later gates may depend on earlier enrichment.
    The order in `PIPELINE` is therefore load-bearing and covered by a test.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..config import KernelConfig
from ..crypto import KeyRegistry, PublicKeyRecord
from ..errors import Reason
from ..models import CartMandate, Decision, GateResult, IntentMandate, KernelRequest, ProposedAction
from ..store import Store


@dataclass
class GateContext:
    cfg: KernelConfig
    store: Store
    registry: KeyRegistry
    request: KernelRequest
    now: int
    clock: Callable[[], int]

    action: ProposedAction | None = None
    intent: IntentMandate | None = None
    cart: CartMandate | None = None

    agent_key: PublicKeyRecord | None = None
    user_key: PublicKeyRecord | None = None
    merchant_key: PublicKeyRecord | None = None

    computed_cart_hash: str | None = None
    idempotency_key: str | None = None
    replayed_result: dict[str, Any] | None = None
    claimed_idem: bool = False
    scratch: dict[str, Any] = field(default_factory=dict)


class Gate(Protocol):
    name: str
    ordinal: int

    def __call__(self, ctx: GateContext) -> GateResult: ...


def ok(gate: str, ordinal: int, detail: str = "", **evidence: Any) -> GateResult:
    return GateResult(gate=gate, ordinal=ordinal, decision=Decision.ALLOW,
                      reason=Reason.OK, detail=detail, evidence=evidence)


def deny(gate: str, ordinal: int, reason: Reason, detail: str = "", **evidence: Any) -> GateResult:
    return GateResult(gate=gate, ordinal=ordinal, decision=Decision.DENY,
                      reason=reason, detail=detail, evidence=evidence)


def timed(fn: Callable[[GateContext], GateResult]) -> Callable[[GateContext], GateResult]:
    def wrapper(ctx: GateContext) -> GateResult:
        t0 = time.perf_counter_ns()
        res = fn(ctx)
        return res.model_copy(update={"elapsed_us": (time.perf_counter_ns() - t0) // 1000})
    wrapper.__name__ = fn.__name__
    return wrapper
