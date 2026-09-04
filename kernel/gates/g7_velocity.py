"""Gate 7 — Velocity, circuit breaker, kill switch.

This is the gate that stops a *correct* agent from being an expensive agent: a
retry storm made of individually-valid requests.

Edge cases handled here:
  * Kill switch, checked from config AND from a live DB flag so an operator can
    stop the world without a redeploy. Compensating refunds are still allowed —
    a kill switch that traps customer money is a worse incident.
  * Per-mandate transaction count against `max_transactions`, using the same
    counter the reservation increments, so failed-and-released attempts do not
    burn the user's allowance.
  * Per-mandate rate limit (rolling 60s) and an independent global rate limit,
    so one runaway mandate cannot exhaust provider quota for everyone.
  * Circuit breaker: N consecutive denials opens the breaker for a cooldown.
    This is what converts "the agent is confused" into "the agent is stopped"
    without a human in the loop.
  * Rate events are recorded only for admitted requests, so a denial storm
    cannot itself trip the rate limiter into hiding real traffic.
"""
from __future__ import annotations

from ..errors import Reason
from ..models import AttemptClass
from .base import GateContext, GateResult, deny, ok, timed

NAME, ORDINAL = "velocity", 7


@timed
def gate(ctx: GateContext) -> GateResult:
    assert ctx.action and ctx.intent
    mid = ctx.intent.mandate_id
    is_refund = ctx.action.attempt_class is AttemptClass.COMPENSATION

    kill = ctx.cfg.kill_switch or ctx.store.flag_get("kill_switch", "0") == "1"
    if kill and not is_refund:
        return deny(NAME, ORDINAL, Reason.VEL_KILL_SWITCH,
                    "global kill switch engaged; only compensating refunds are permitted")

    state = ctx.store.spend_state(mid)

    if state["breaker_until"] > ctx.now and not is_refund:
        return deny(NAME, ORDINAL, Reason.VEL_BREAKER_OPEN,
                    f"circuit breaker open for another {state['breaker_until'] - ctx.now}s",
                    breaker_until=state["breaker_until"])

    if not is_refund and state["txn_count"] >= ctx.intent.constraints.max_transactions:
        return deny(NAME, ORDINAL, Reason.VEL_TXN_COUNT,
                    f"{state['txn_count']} transactions already used of "
                    f"{ctx.intent.constraints.max_transactions}",
                    used=state["txn_count"], allowed=ctx.intent.constraints.max_transactions)

    per_mandate = ctx.store.rate_count(f"mandate:{mid}")
    if per_mandate >= ctx.intent.constraints.rate_per_minute:
        return deny(NAME, ORDINAL, Reason.VEL_RATE_LIMIT,
                    f"{per_mandate} admitted requests in the last 60s exceeds "
                    f"{ctx.intent.constraints.rate_per_minute}/min", observed=per_mandate)

    global_rate = ctx.store.rate_count("global")
    if global_rate >= ctx.cfg.global_rate_per_minute:
        return deny(NAME, ORDINAL, Reason.VEL_RATE_LIMIT,
                    f"global rate {global_rate}/min exceeds {ctx.cfg.global_rate_per_minute}",
                    observed=global_rate, scope="global")

    ctx.scratch["record_rate"] = True
    return ok(NAME, ORDINAL, "within velocity limits and breaker closed",
              per_mandate_rpm=per_mandate, global_rpm=global_rate,
              txn_used=state["txn_count"])
