"""Mandate Kernel — deterministic policy engine for agent-initiated payments.

Public surface:
    from kernel import Kernel, Executor, Store, KeyRegistry, KernelConfig
"""
from .capability import CapabilityError, mint, redeem  # noqa: F401
from .config import KernelConfig  # noqa: F401
from .crypto import KeyPair, KeyRegistry, KeyRole, sign_payload, verify_envelope  # noqa: F401
from .errors import KernelDenied, Reason  # noqa: F401
from .executor import ExecutionOutcome, Executor  # noqa: F401
from .models import (  # noqa: F401
    ActionKind,
    AttemptClass,
    CartItem,
    CartMandate,
    Constraints,
    Decision,
    Envelope,
    IntentMandate,
    KernelRequest,
    ProposedAction,
    Verdict,
)
from .pipeline import Kernel  # noqa: F401
from .store import Store  # noqa: F401

__all__ = [
    "Kernel", "Executor", "Store", "KeyRegistry", "KeyPair", "KeyRole", "KernelConfig",
    "Reason", "KernelDenied", "Decision", "Verdict", "KernelRequest", "Envelope",
    "IntentMandate", "CartMandate", "CartItem", "Constraints", "ProposedAction",
    "ActionKind", "AttemptClass", "ExecutionOutcome", "sign_payload", "verify_envelope",
    "mint", "redeem", "CapabilityError",
]
__version__ = "1.0.0"
