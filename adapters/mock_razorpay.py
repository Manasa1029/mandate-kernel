"""Deterministic Razorpay stand-in with scriptable failures.

Why this exists: a payment demo that needs the internet is a demo that fails on
stage, and a red-team suite that hits a live sandbox is neither reproducible nor
polite. The mock reproduces the *shapes* Razorpay returns (`order_...`,
`pay_...`, `rfnd_...`, `plink_...`, `status`, `amount` in paise) and the failure
*classes* that matter.

Failure scripting:

    provider.script([
        Fail("create_order", ProviderRetriable, "gateway_timeout"),
        Fail("create_order", None),          # second attempt succeeds
    ])

`notes.idem_key` is stamped on every create so `find_by_idempotency` can
reconcile after an unknown-state error — the same trick used against the real API.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from .base import (
    PaymentProvider,
    ProviderRejected,
    ProviderResult,
    ProviderRetriable,
    ProviderUnknownState,
)

_counter = itertools.count(1)


def _pid(prefix: str) -> str:
    return f"{prefix}_{next(_counter):014d}"


@dataclass
class Fail:
    op: str
    error: type[Exception] | None = None
    code: str = ""
    landed: bool = False  # for UnknownState: did the write actually take effect?


class MockRazorpay(PaymentProvider):
    name = "mock"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._script: list[Fail] = []
        self._orders: dict[str, dict[str, Any]] = {}
        self._payments: dict[str, dict[str, Any]] = {}
        self._refunds: dict[str, dict[str, Any]] = {}
        self._by_idem: dict[str, str] = {}

    # ------------------------------------------------------------ scripting

    def script(self, plan: list[Fail]) -> None:
        self._script = list(plan)

    def _next_failure(self, op: str) -> Fail | None:
        for i, f in enumerate(self._script):
            if f.op == op:
                return self._script.pop(i)
        return None

    def _maybe_fail(self, op: str, *, on_land: Any = None) -> None:
        f = self._next_failure(op)
        if f is None or f.error is None:
            return
        if f.error is ProviderUnknownState and f.landed and on_land is not None:
            on_land()
        raise f.error(f"scripted {op} failure", code=f.code or "scripted")

    # ------------------------------------------------------------------ API

    def create_order(self, *, amount_paise: int, receipt: str, idempotency_key: str,
                     notes: dict[str, str]) -> ProviderResult:
        self.calls.append({"op": "create_order", "amount": amount_paise, "idem": idempotency_key})

        if idempotency_key in self._by_idem:  # provider-side dedupe, like a real gateway
            oid = self._by_idem[idempotency_key]
            o = self._orders[oid]
            return ProviderResult(True, oid, o["status"], o["amount"], "order", o)

        oid = _pid("order")
        record = {"id": oid, "amount": amount_paise, "currency": "INR", "status": "created",
                  "receipt": receipt, "notes": {**notes, "idem_key": idempotency_key},
                  "attempts": 0}

        def land() -> None:
            self._orders[oid] = record
            self._by_idem[idempotency_key] = oid

        self._maybe_fail("create_order", on_land=land)
        land()
        return ProviderResult(True, oid, "created", amount_paise, "order", record)

    def create_payment_link(self, *, amount_paise: int, description: str, idempotency_key: str,
                            notes: dict[str, str], upi_only: bool = False) -> ProviderResult:
        self.calls.append({"op": "create_payment_link", "amount": amount_paise, "idem": idempotency_key})
        if idempotency_key in self._by_idem:
            lid = self._by_idem[idempotency_key]
            r = self._orders[lid]
            return ProviderResult(True, lid, r["status"], r["amount"], "payment_link", r,
                                  short_url=r.get("short_url"))
        lid = _pid("plink")
        record = {"id": lid, "amount": amount_paise, "currency": "INR", "status": "created",
                  "description": description, "upi_link": upi_only,
                  "short_url": f"https://rzp.io/i/{lid[-8:]}",
                  "notes": {**notes, "idem_key": idempotency_key}}

        def land() -> None:
            self._orders[lid] = record
            self._by_idem[idempotency_key] = lid

        self._maybe_fail("create_payment_link", on_land=land)
        land()
        return ProviderResult(True, lid, "created", amount_paise, "payment_link", record,
                              short_url=record["short_url"])

    def simulate_customer_payment(self, order_id: str, *, authorize_only: bool = True,
                                  fail: bool = False) -> ProviderResult:
        """Test-mode convenience: pretend the customer paid (or failed to)."""
        order = self._orders[order_id]
        pid = _pid("pay")
        status = "failed" if fail else ("authorized" if authorize_only else "captured")
        rec = {"id": pid, "order_id": order_id, "amount": order["amount"], "status": status,
               "method": "upi", "error_code": "BAD_REQUEST_ERROR" if fail else None,
               "notes": order["notes"]}
        self._payments[pid] = rec
        return ProviderResult(not fail, pid, status, order["amount"], "payment", rec)

    def capture_payment(self, *, payment_id: str, amount_paise: int) -> ProviderResult:
        self.calls.append({"op": "capture_payment", "payment_id": payment_id, "amount": amount_paise})
        self._maybe_fail("capture_payment")
        rec = self._payments.get(payment_id)
        if rec is None:
            raise ProviderRejected("unknown payment", code="NOT_FOUND")
        if rec["status"] == "captured":
            return ProviderResult(True, payment_id, "captured", rec["amount"], "payment", rec)
        if rec["status"] != "authorized":
            raise ProviderRejected(f"cannot capture from {rec['status']}", code="INVALID_STATE")
        if amount_paise != rec["amount"]:
            raise ProviderRejected("capture amount must equal authorized amount", code="AMOUNT_MISMATCH")
        rec["status"] = "captured"
        return ProviderResult(True, payment_id, "captured", rec["amount"], "payment", rec)

    def create_refund(self, *, payment_id: str, amount_paise: int, idempotency_key: str) -> ProviderResult:
        self.calls.append({"op": "create_refund", "payment_id": payment_id, "amount": amount_paise,
                           "idem": idempotency_key})
        if idempotency_key in self._by_idem:
            rid = self._by_idem[idempotency_key]
            r = self._refunds[rid]
            return ProviderResult(True, rid, r["status"], r["amount"], "refund", r)
        self._maybe_fail("create_refund")
        rec = self._payments.get(payment_id)
        if rec is None or rec["status"] != "captured":
            raise ProviderRejected("only captured payments can be refunded", code="INVALID_STATE")
        already = sum(r["amount"] for r in self._refunds.values() if r["payment_id"] == payment_id)
        if already + amount_paise > rec["amount"]:
            raise ProviderRejected("refund exceeds captured amount", code="AMOUNT_EXCEEDS")
        rid = _pid("rfnd")
        r = {"id": rid, "payment_id": payment_id, "amount": amount_paise, "status": "processed",
             "notes": {"idem_key": idempotency_key}}
        self._refunds[rid] = r
        self._by_idem[idempotency_key] = rid
        return ProviderResult(True, rid, "processed", amount_paise, "refund", r)

    def fetch_payment(self, *, payment_id: str) -> ProviderResult:
        rec = self._payments.get(payment_id)
        if rec is None:
            raise ProviderRejected("unknown payment", code="NOT_FOUND")
        return ProviderResult(rec["status"] != "failed", payment_id, rec["status"],
                              rec["amount"], "payment", rec)

    def find_by_idempotency(self, *, idempotency_key: str) -> ProviderResult | None:
        oid = self._by_idem.get(idempotency_key)
        if oid is None:
            return None
        for bucket, kind in ((self._orders, "order"), (self._refunds, "refund")):
            if oid in bucket:
                rec = bucket[oid]
                return ProviderResult(True, oid, rec["status"], rec["amount"], kind, rec)
        return None
