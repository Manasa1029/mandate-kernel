"""Deterministic demo identities shared by the kernel API, the seller and the agent.

In production these are three separate key custodians: the user's passkey/HSM,
the agent's workload identity, and the merchant's signing key. For a 48-hour
build they are derived from one seed so every process agrees on key ids without a
key-distribution service — and the seed is the ONLY thing that is fake.

`KEY_SEED` must be set in any deployment that matters; the default exists so
`make demo` works on a clean clone.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from kernel.crypto import KeyPair, KeyRegistry, KeyRole

SEED = os.environ.get("KEY_SEED", "mandate-kernel-demo-seed-do-not-use-in-prod").encode()

USER_SUBJECT = os.environ.get("DEMO_USER", "user_nikitha")
AGENT_SUBJECT = os.environ.get("DEMO_AGENT", "agent_pantry_bot")
MERCHANT_ID = os.environ.get("DEMO_MERCHANT", "acme_pantry")
MERCHANT_PAYEE = os.environ.get("DEMO_PAYEE", "acmepantry@hdfcbank")


@dataclass(frozen=True)
class Identities:
    user: KeyPair
    agent: KeyPair
    merchant: KeyPair
    rogue: KeyPair
    registry: KeyRegistry


def load_identities() -> Identities:
    user = KeyPair.from_seed(KeyRole.USER, USER_SUBJECT, SEED + b":user")
    agent = KeyPair.from_seed(KeyRole.AGENT, AGENT_SUBJECT, SEED + b":agent")
    merchant = KeyPair.from_seed(KeyRole.MERCHANT, MERCHANT_ID, SEED + b":merchant")
    # A registered-but-not-delegated agent. Its existence is the point: it proves
    # Gate 2 rejects on *delegation*, not merely on "is this key known to us".
    rogue = KeyPair.from_seed(KeyRole.AGENT, "agent_rogue", SEED + b":rogue")

    registry = KeyRegistry()
    for kp in (user, agent, merchant, rogue):
        registry.register(kp)
    return Identities(user=user, agent=agent, merchant=merchant, rogue=rogue, registry=registry)


IDENTITIES = load_identities()
