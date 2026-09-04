"""Signable data structures + the kernel's request/verdict types.

Everything here is `extra="forbid"` and `strict`-ish on purpose: Gate 1 *is* this
module. An LLM that invents a field, sends "4000" as a string, or slips a float
into an amount is rejected before any business logic runs.

Timestamps are integer epoch seconds — floats are unsignable (see canonical.py)
and millisecond precision buys nothing here.
"""
from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from .money import CURRENCY, MAX_PAISE

Paise = Annotated[StrictInt, Field(ge=0, le=MAX_PAISE)]
PosPaise = Annotated[StrictInt, Field(gt=0, le=MAX_PAISE)]


def now_s() -> int:
    return int(time.time())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    def signable(self) -> dict[str, Any]:
        """Canonical dict used for signing/hashing — excludes nothing, sorts nothing.

        Sorting happens in canonical_bytes; `mode="json"` guarantees enums become
        strings so a Python-side enum change cannot invalidate old signatures.
        """
        return self.model_dump(mode="json")


class ActionKind(StrEnum):
    CREATE_ORDER = "create_order"
    CREATE_PAYMENT_LINK = "create_payment_link"
    CAPTURE_PAYMENT = "capture_payment"
    CREATE_REFUND = "create_refund"


class AttemptClass(StrEnum):
    INITIAL = "initial"          # first try for this cart
    RETRY = "retry"              # same instrument, same idempotency key
    ESCALATION = "escalation"    # different instrument -> new idempotency key
    COMPENSATION = "compensation"  # refund leg of a saga


class Constraints(Base):
    max_total_paise: PosPaise
    max_per_txn_paise: PosPaise
    max_transactions: Annotated[StrictInt, Field(ge=1, le=1000)]
    rate_per_minute: Annotated[StrictInt, Field(ge=1, le=600)] = 6
    allowed_merchants: tuple[StrictStr, ...] = ()
    allowed_payees: tuple[StrictStr, ...] = ()
    allowed_skus: tuple[StrictStr, ...] = ()
    allowed_categories: tuple[StrictStr, ...] = ()
    denied_skus: tuple[StrictStr, ...] = ()
    denied_payees: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def _coherent(self) -> "Constraints":
        if self.max_per_txn_paise > self.max_total_paise:
            raise ValueError("max_per_txn_paise cannot exceed max_total_paise")
        if not self.allowed_merchants:
            raise ValueError("an intent with no merchant allowlist authorises everything")
        if not self.allowed_payees:
            raise ValueError("an intent with no payee allowlist authorises everything")
        if not (self.allowed_skus or self.allowed_categories):
            raise ValueError("intent must scope either SKUs or categories")
        return self


class IntentMandate(Base):
    """AP2-shaped Intent Mandate: what the user authorised, and its boundaries."""

    type: Literal["IntentMandate"] = "IntentMandate"
    version: Literal[1] = 1
    mandate_id: StrictStr
    subject: StrictStr                       # user id — the payer of record
    delegated_agents: tuple[StrictStr, ...]  # agent key_ids permitted to propose
    human_present: StrictBool
    prompt_playback: StrictStr               # natural-language echo of what was approved
    currency: Literal["INR"] = CURRENCY
    constraints: Constraints
    issued_at: StrictInt
    expires_at: StrictInt
    nonce: StrictStr

    @model_validator(mode="after")
    def _window(self) -> "IntentMandate":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.expires_at - self.issued_at > 30 * 24 * 3600:
            raise ValueError("intent TTL longer than 30 days is not bounded autonomy")
        if not self.delegated_agents:
            raise ValueError("no delegated agent — nothing may propose against this mandate")
        return self


class CartItem(Base):
    sku: StrictStr
    name: StrictStr
    category: StrictStr
    qty: Annotated[StrictInt, Field(gt=0, le=100_000)]
    unit_price_paise: PosPaise
    tax_paise: Paise = 0


class CartMandate(Base):
    """Merchant-signed price lock. The merchant cannot change price after issuing."""

    type: Literal["CartMandate"] = "CartMandate"
    version: Literal[1] = 1
    cart_id: StrictStr
    merchant_id: StrictStr
    intent_ref: StrictStr | None = None
    currency: Literal["INR"] = CURRENCY
    items: tuple[CartItem, ...]
    subtotal_paise: PosPaise
    tax_paise: Paise
    shipping_paise: Paise
    total_paise: PosPaise
    payee: StrictStr
    quoted_at: StrictInt
    price_valid_until: StrictInt
    nonce: StrictStr

    @field_validator("items")
    @classmethod
    def _non_empty(cls, v: tuple[CartItem, ...]) -> tuple[CartItem, ...]:
        if not v:
            raise ValueError("cart must have at least one item")
        if len(v) > 200:
            raise ValueError("cart too large")
        skus = [i.sku for i in v]
        if len(set(skus)) != len(skus):
            raise ValueError("duplicate SKU lines — merge before quoting")
        return v

    @model_validator(mode="after")
    def _window(self) -> "CartMandate":
        if self.price_valid_until <= self.quoted_at:
            raise ValueError("price_valid_until must be after quoted_at")
        return self


class ProposedAction(Base):
    """What the agent asks for. Signed by the agent, never by the user."""

    type: Literal["ProposedAction"] = "ProposedAction"
    version: Literal[1] = 1
    action_id: StrictStr
    action: ActionKind
    amount_paise: PosPaise
    currency: Literal["INR"] = CURRENCY
    merchant_id: StrictStr
    payee: StrictStr
    intent_ref: StrictStr
    cart_ref: StrictStr
    cart_hash: StrictStr
    attempt: Annotated[StrictInt, Field(ge=1, le=10)]
    attempt_class: AttemptClass
    client_nonce: StrictStr
    rationale: StrictStr = ""       # advisory only; the kernel never reads it for decisions
    reference_id: StrictStr | None = None  # payment/order id for capture & refund actions

    @model_validator(mode="after")
    def _needs_reference(self) -> "ProposedAction":
        if self.action in (ActionKind.CAPTURE_PAYMENT, ActionKind.CREATE_REFUND) and not self.reference_id:
            raise ValueError(f"{self.action} requires reference_id")
        if self.action == ActionKind.CREATE_REFUND and self.attempt_class != AttemptClass.COMPENSATION:
            raise ValueError("refunds must be proposed as compensation")
        return self


class Envelope(BaseModel):
    """Signed wrapper as it arrives over the wire — validated by Gate 2, not Gate 1."""

    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any]
    sig: dict[str, Any]


class KernelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Envelope
    intent: Envelope
    cart: Envelope | None = None


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate: StrictStr
    ordinal: StrictInt
    decision: Decision
    reason: StrictStr
    detail: StrictStr = ""
    elapsed_us: StrictInt = 0
    evidence: dict[str, Any] = Field(default_factory=dict)


class Capability(BaseModel):
    """Single-use, single-amount, single-payee execution token."""

    model_config = ConfigDict(extra="forbid")
    token: StrictStr
    action_id: StrictStr
    mandate_id: StrictStr
    idempotency_key: StrictStr
    action: ActionKind
    amount_paise: PosPaise
    currency: Literal["INR"] = CURRENCY
    merchant_id: StrictStr
    payee: StrictStr
    reference_id: StrictStr | None = None
    issued_at: StrictInt
    expires_at: StrictInt


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Decision
    reason: StrictStr
    action_id: StrictStr | None = None
    mandate_id: StrictStr | None = None
    gates: list[GateResult] = Field(default_factory=list)
    capability: Capability | None = None
    replayed_result: dict[str, Any] | None = None
    ledger_seq: StrictInt | None = None
    total_elapsed_us: StrictInt = 0

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW
