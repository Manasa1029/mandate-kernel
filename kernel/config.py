"""Kernel configuration. Everything tunable, nothing magic, all of it logged."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class KernelConfig:
    # Gate 3 — clock skew we tolerate on inbound signed timestamps.
    clock_skew_s: int = 30
    # Gate 3 — how long a nonce stays in the replay cache.
    nonce_ttl_s: int = 24 * 3600
    # Gate 7 — consecutive denials before the per-mandate breaker opens.
    breaker_denial_threshold: int = 5
    breaker_cooldown_s: int = 300
    # Gate 7 — global ceiling independent of any mandate.
    global_rate_per_minute: int = 120
    # Capability token lifetime. Short by design: it must not survive a coffee break.
    capability_ttl_s: int = 90
    # Executor
    max_attempts_per_cart: int = 3
    provider_timeout_s: float = 8.0
    # Ops
    kill_switch: bool = False
    db_path: str = "kernel.db"
    razorpay_mode: str = "mock"   # mock | rest  (aliases: razorpay, test, live_test)

    @classmethod
    def from_env(cls) -> "KernelConfig":
        return cls(
            clock_skew_s=_int("KERNEL_CLOCK_SKEW_S", 30),
            nonce_ttl_s=_int("KERNEL_NONCE_TTL_S", 24 * 3600),
            breaker_denial_threshold=_int("KERNEL_BREAKER_THRESHOLD", 5),
            breaker_cooldown_s=_int("KERNEL_BREAKER_COOLDOWN_S", 300),
            global_rate_per_minute=_int("KERNEL_GLOBAL_RPM", 120),
            capability_ttl_s=_int("KERNEL_CAPABILITY_TTL_S", 90),
            max_attempts_per_cart=_int("KERNEL_MAX_ATTEMPTS", 3),
            provider_timeout_s=float(os.getenv("KERNEL_PROVIDER_TIMEOUT_S", "8.0")),
            kill_switch=_bool("KERNEL_KILL_SWITCH", False),
            db_path=os.getenv("KERNEL_DB_PATH", "kernel.db"),
            razorpay_mode=os.getenv("RAZORPAY_MODE", "mock"),
        )
