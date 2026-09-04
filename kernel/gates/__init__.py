"""The pipeline order is the security model. Changing it is a reviewable event.

Rationale for this exact order:
  1 schema      — cheapest, and everything below assumes parsed types
  2 signature   — never spend CPU on unauthenticated content beyond parsing
  3 freshness   — reject stale/replayed input before touching money state
  4 budget      — read-only projection of spend
  5 allowlist   — who/what, independent of how much
  6 price       — recompute before committing to an amount
  7 velocity    — rate/breaker checks after we know the request is legitimate,
                  so denial storms don't poison the rate window
  8 idempotency — LAST, because it takes a lock; taking it earlier would leak
                  in-flight locks for requests that were never going to be legal
"""
from __future__ import annotations

from . import (
    g1_schema,
    g2_signature,
    g3_freshness,
    g4_budget,
    g5_allowlist,
    g6_price,
    g7_velocity,
    g8_idempotency,
)
from .base import GateContext, deny, ok, timed  # noqa: F401

PIPELINE = (
    g1_schema.gate,
    g2_signature.gate,
    g3_freshness.gate,
    g4_budget.gate,
    g5_allowlist.gate,
    g6_price.gate,
    g7_velocity.gate,
    g8_idempotency.gate,
)

GATE_NAMES = (
    "schema", "signature", "freshness", "budget",
    "allowlist", "price_binding", "velocity", "idempotency",
)
