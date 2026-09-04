"""The kernel entry point: run the gates, decide, record, mint.

Invariants this function guarantees, and which the tests assert:

  I1  The entire evaluation runs inside one `BEGIN IMMEDIATE` transaction, so a
      concurrent evaluation for the same mandate cannot interleave between the
      budget check (G4) and the reservation.
  I2  Every request produces exactly one ledger entry, allow or deny.
  I3  A denial never leaves a reservation, a rate event, or an in-flight
      idempotency claim behind.
  I4  An unexpected exception inside any gate becomes a DENY, never an ALLOW.
  I5  `capability` is non-None if and only if `decision == ALLOW`.
"""
from __future__ import annotations

import logging
import time

from . import capability as cap_mod
from .config import KernelConfig
from .crypto import KeyRegistry
from .errors import Reason
from .gates import PIPELINE
from .gates.base import GateContext
from .models import Decision, GateResult, KernelRequest, Verdict, now_s
from .store import Store

log = logging.getLogger("kernel")

# Denials that are protocol outcomes rather than policy violations: they must not
# advance the circuit breaker, or an at-least-once queue would trip it by design.
_NON_PUNITIVE = {Reason.IDEM_REPLAYED, Reason.IDEM_IN_FLIGHT}


class Kernel:
    def __init__(self, store: Store, registry: KeyRegistry, cfg: KernelConfig | None = None) -> None:
        self.store = store
        self.registry = registry
        self.cfg = cfg or KernelConfig()

    def evaluate(self, request: KernelRequest) -> Verdict:
        t0 = time.perf_counter_ns()
        results: list[GateResult] = []
        ctx = GateContext(cfg=self.cfg, store=self.store, registry=self.registry,
                          request=request, now=now_s(), clock=now_s)

        with self.store.transaction():
            failure: GateResult | None = None
            for fn in PIPELINE:
                try:
                    res = fn(ctx)
                except Exception as exc:  # I4 — fail closed, loudly
                    log.exception("gate crashed")
                    res = GateResult(gate=getattr(fn, "__name__", "unknown"), ordinal=len(results) + 1,
                                     decision=Decision.DENY, reason=Reason.SCHEMA_INVALID,
                                     detail=f"gate raised {type(exc).__name__}: {exc}")
                results.append(res)
                if res.decision is Decision.DENY:
                    failure = res
                    break

            mandate_id = ctx.intent.mandate_id if ctx.intent else None
            action_id = ctx.action.action_id if ctx.action else None

            if failure is not None:
                # I3 — unwind anything a gate claimed before the failure.
                if ctx.claimed_idem and ctx.idempotency_key:
                    self.store.idem_release(ctx.idempotency_key)
                if mandate_id and failure.reason not in _NON_PUNITIVE:
                    self.store.note_denial(mandate_id, self.cfg.breaker_denial_threshold,
                                           self.cfg.breaker_cooldown_s)
                verdict = Verdict(
                    decision=Decision.DENY, reason=str(failure.reason), action_id=action_id,
                    mandate_id=mandate_id, gates=results, replayed_result=ctx.replayed_result,
                    total_elapsed_us=(time.perf_counter_ns() - t0) // 1000,
                )
                seq, _ = self.store.append("verdict.deny", verdict.model_dump(mode="json"),
                                           mandate_id=mandate_id, action_id=action_id)
                return verdict.model_copy(update={"ledger_seq": seq})

            assert ctx.action and ctx.intent and ctx.idempotency_key
            reserve = ctx.scratch.get("reserve_paise")
            if reserve:
                self.store.reserve(ctx.intent.mandate_id, reserve)
            if ctx.scratch.get("record_rate"):
                self.store.rate_record(f"mandate:{ctx.intent.mandate_id}")
                self.store.rate_record("global")
            self.store.note_success(ctx.intent.mandate_id)

            cap = cap_mod.mint(self.store, self.cfg, ctx.action, ctx.intent.mandate_id,
                               ctx.idempotency_key)
            verdict = Verdict(
                decision=Decision.ALLOW, reason=str(Reason.OK), action_id=ctx.action.action_id,
                mandate_id=ctx.intent.mandate_id, gates=results, capability=cap,
                total_elapsed_us=(time.perf_counter_ns() - t0) // 1000,
            )
            payload = verdict.model_dump(mode="json")
            # Never write the bearer token to the audit log; store its digest instead.
            payload["capability"]["token"] = "cap_***" + cap.token[-6:]
            payload["prompt_playback"] = ctx.intent.prompt_playback
            payload["reserved_paise"] = reserve or 0
            seq, _ = self.store.append("verdict.allow", payload,
                                       mandate_id=ctx.intent.mandate_id, action_id=ctx.action.action_id)
            return verdict.model_copy(update={"ledger_seq": seq})
