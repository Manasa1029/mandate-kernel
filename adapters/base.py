"""Provider abstraction. The kernel never imports httpx or razorpay directly.

Two implementations satisfy this interface:
  * `MockRazorpay`      — deterministic, offline, scriptable failures (tests, CI, demo)
  * `RazorpayRestClient`— real test-mode HTTP calls

Error taxonomy matters more than the happy path. The executor branches on these
three classes and nothing else:

  ProviderRejected      definitive NO from the provider. Safe to surface.
  ProviderRetriable     transient (5xx, 429, connection reset). Safe to retry
                        with the SAME idempotency key.
  ProviderUnknownState  we sent the request and do not know if it landed
                        (timeout after write, socket death mid-response). NEVER
                        retry blindly: reconcile by fetching first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(Exception):
    def __init__(self, message: str, code: str = "", raw: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


class ProviderRejected(ProviderError):
    """Definitive failure — insufficient funds, invalid VPA, blocked card."""


class ProviderRetriable(ProviderError):
    """Transient failure — retry with the same idempotency key."""


class ProviderUnknownState(ProviderError):
    """We do not know whether the write landed. Reconcile, never blind-retry."""


@dataclass
class ProviderResult:
    ok: bool
    provider_id: str
    status: str
    amount_paise: int
    kind: str
    raw: dict[str, Any] = field(default_factory=dict)
    short_url: str | None = None


class PaymentProvider(Protocol):
    name: str

    def create_order(self, *, amount_paise: int, receipt: str, idempotency_key: str,
                     notes: dict[str, str]) -> ProviderResult: ...

    def create_payment_link(self, *, amount_paise: int, description: str, idempotency_key: str,
                            notes: dict[str, str], upi_only: bool = False) -> ProviderResult: ...

    def capture_payment(self, *, payment_id: str, amount_paise: int) -> ProviderResult: ...

    def create_refund(self, *, payment_id: str, amount_paise: int,
                      idempotency_key: str) -> ProviderResult: ...

    def fetch_payment(self, *, payment_id: str) -> ProviderResult: ...

    def find_by_idempotency(self, *, idempotency_key: str) -> ProviderResult | None:
        """Reconciliation hook for ProviderUnknownState.

        Razorpay has no first-class idempotency header on every endpoint, so we
        stamp our key into `notes.idem_key` on create and search notes on
        recovery. This is exactly how you avoid double-charging after a timeout.
        """
        ...
