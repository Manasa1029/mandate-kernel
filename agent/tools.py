"""Agent tools. In-process by default, HTTP-capable by construction.

The tool list is the agent's *entire* capability surface, and it is deliberately
lopsided: the agent can search, quote, propose and read state. It cannot pay.
`propose_payment` returns a verdict; only `execute_capability` moves money, and
it requires a token the kernel minted seconds earlier for one exact amount.

Running in-process (rather than over HTTP) for the demo removes a whole class of
"it worked on my laptop" failures during a 5-minute pitch, and the seam is a
single dataclass — `Runtime` — so swapping in HTTP is a constructor change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adapters import MockRazorpay, build_provider
from bootstrap import IDENTITIES, MERCHANT_ID, MERCHANT_PAYEE
from kernel.canonical import digest
from kernel.config import KernelConfig
from kernel.crypto import sign_payload
from kernel.executor import ExecutionOutcome, Executor
from kernel.models import (
    ActionKind,
    AttemptClass,
    CartItem,
    CartMandate,
    Constraints,
    Envelope,
    IntentMandate,
    KernelRequest,
    ProposedAction,
    Verdict,
    new_id,
    now_s,
)
from kernel.pipeline import Kernel
from kernel.store import Store
from seller import catalog

QUOTE_TTL_S = 300


@dataclass
class Runtime:
    store: Store
    kernel: Kernel
    executor: Executor
    cfg: KernelConfig
    provider: Any
    issued_quotes: dict[str, CartMandate] = field(default_factory=dict)
    pending: dict[str, tuple[Verdict, ProposedAction]] = field(default_factory=dict)

    @classmethod
    def local(cls, db_path: str = ":memory:", **cfg_kw: Any) -> "Runtime":
        cfg = KernelConfig(db_path=db_path, **cfg_kw)
        store = Store(db_path)
        provider = build_provider(cfg.razorpay_mode, timeout=cfg.provider_timeout_s)
        kernel = Kernel(store, IDENTITIES.registry, cfg)
        return cls(store=store, kernel=kernel, executor=Executor(store, provider, cfg),
                   cfg=cfg, provider=provider)


# ------------------------------------------------------------------ read tools

def search_catalog(query: str = "", category: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """UNTRUSTED DATA. Returns seller-authored text exactly as published."""
    return [catalog.public_view(p) for p in catalog.search(query, category, limit)]


def mandate_state(rt: Runtime, mandate_id: str) -> dict[str, int]:
    return rt.store.spend_state(mandate_id)


# --------------------------------------------------------------- mandate tools

def issue_intent(rt: Runtime, *, playback: str, max_total_paise: int, max_per_txn_paise: int,
                 max_transactions: int = 3, categories: tuple[str, ...] = ("groceries",),
                 skus: tuple[str, ...] = (), merchants: tuple[str, ...] = (MERCHANT_ID,),
                 payees: tuple[str, ...] = (MERCHANT_PAYEE,), denied_skus: tuple[str, ...] = (),
                 ttl_s: int = 3600, human_present: bool = True,
                 rate_per_minute: int = 6) -> tuple[IntentMandate, Envelope]:
    """Stand-in for the user's signing device (passkey / secure element)."""
    t = now_s()
    intent = IntentMandate(
        mandate_id=new_id("mnd"), subject=IDENTITIES.user.subject,
        delegated_agents=(IDENTITIES.agent.key_id,), human_present=human_present,
        prompt_playback=playback,
        constraints=Constraints(max_total_paise=max_total_paise, max_per_txn_paise=max_per_txn_paise,
                                max_transactions=max_transactions, rate_per_minute=rate_per_minute,
                                allowed_merchants=merchants, allowed_payees=payees,
                                allowed_skus=skus, allowed_categories=categories,
                                denied_skus=denied_skus),
        issued_at=t, expires_at=t + ttl_s, nonce=new_id("n"))
    env = Envelope.model_validate(sign_payload(IDENTITIES.user, intent.signable()))
    rt.store.append("mandate.issued",
                    {"mandate_id": intent.mandate_id, "prompt_playback": playback,
                     "constraints": intent.constraints.model_dump(mode="json"),
                     "expires_at": intent.expires_at},
                    mandate_id=intent.mandate_id)
    return intent, env


def get_quote(rt: Runtime, items: list[dict[str, Any]], intent_ref: str | None = None,
              *, quote_ttl_s: int = QUOTE_TTL_S) -> tuple[CartMandate, Envelope]:
    """Merchant-signed price lock. Raises ValueError on unknown/duplicate SKUs."""
    lines: list[CartItem] = []
    subtotal = tax = 0
    seen: set[str] = set()
    for line in items:
        sku, qty = str(line["sku"]), int(line["qty"])
        if sku in seen:
            raise ValueError(f"duplicate sku {sku}")
        seen.add(sku)
        p = catalog.BY_SKU.get(sku)
        if p is None:
            raise ValueError(f"unknown sku {sku}")
        if not 0 < qty <= 50:
            raise ValueError(f"qty out of range for {sku}")
        gross = p.price_paise * qty
        line_tax = gross * p.tax_bp // 10_000
        subtotal += gross
        tax += line_tax
        lines.append(CartItem(sku=p.sku, name=p.name, category=p.category, qty=qty,
                              unit_price_paise=p.price_paise, tax_paise=line_tax))
    shipping = 0 if subtotal >= 50_000 else 4_000
    t = now_s()
    cart = CartMandate(cart_id=new_id("cart"), merchant_id=MERCHANT_ID, intent_ref=intent_ref,
                       items=tuple(lines), subtotal_paise=subtotal, tax_paise=tax,
                       shipping_paise=shipping, total_paise=subtotal + tax + shipping,
                       payee=MERCHANT_PAYEE, quoted_at=t, price_valid_until=t + quote_ttl_s,
                       nonce=new_id("n"))
    rt.issued_quotes[cart.cart_id] = cart
    return cart, Envelope.model_validate(sign_payload(IDENTITIES.merchant, cart.signable()))


# ------------------------------------------------------------- proposal tools

def build_action(intent: IntentMandate, cart: CartMandate, *, action=ActionKind.CREATE_ORDER,
                 amount_paise: int | None = None, attempt: int = 1,
                 attempt_class=AttemptClass.INITIAL, rationale: str = "",
                 reference_id: str | None = None) -> ProposedAction:
    return ProposedAction(
        action_id=new_id("act"), action=action,
        amount_paise=amount_paise if amount_paise is not None else cart.total_paise,
        merchant_id=cart.merchant_id, payee=cart.payee, intent_ref=intent.mandate_id,
        cart_ref=cart.cart_id, cart_hash=digest(cart.signable()), attempt=attempt,
        attempt_class=attempt_class, client_nonce=new_id("cn"), rationale=rationale[:500],
        reference_id=reference_id)


def propose_payment(rt: Runtime, *, intent_env: Envelope, cart_env: Envelope | None,
                    action: ProposedAction) -> Verdict:
    """Submit a proposal to the kernel. The agent signs; the kernel decides."""
    request = KernelRequest(
        action=Envelope.model_validate(sign_payload(IDENTITIES.agent, action.signable())),
        intent=intent_env, cart=cart_env)
    verdict = rt.kernel.evaluate(request)
    if verdict.allowed and verdict.capability is not None:
        rt.pending[verdict.capability.token] = (verdict, action)
    return verdict


def execute_capability(rt: Runtime, token: str) -> ExecutionOutcome:
    """The only money-moving tool. Consumes a one-shot capability."""
    pending = rt.pending.pop(token, None)
    if pending is None:
        return ExecutionOutcome("failed", "unknown or already-consumed capability token")
    verdict, action = pending
    return rt.executor.execute(verdict, action)


def fulfil(rt: Runtime, cart_id: str) -> dict[str, Any]:
    """Seller-side post-condition. Returning fulfilled=False after money moved is
    what makes the saga necessary rather than decorative."""
    cart = rt.issued_quotes.get(cart_id)
    if cart is None:
        return {"fulfilled": False, "reason": "unknown_cart"}
    blocked = [i.sku for i in cart.items if i.sku in {"SKU-GHEE-BULK"}]
    if blocked:
        return {"fulfilled": False, "reason": "out_of_stock", "skus": blocked}
    return {"fulfilled": True, "shipment_id": new_id("shp")}


def simulate_customer_payment(rt: Runtime, order_id: str, *, fail: bool = False,
                              authorize_only: bool = True) -> dict[str, Any]:
    """Test-mode only: the customer side of the flow, so capture and refund legs
    can be demonstrated without a browser."""
    if not isinstance(rt.provider, MockRazorpay):
        return {"supported": False, "note": "only available with the mock provider"}
    res = rt.provider.simulate_customer_payment(order_id, authorize_only=authorize_only, fail=fail)
    return {"payment_id": res.provider_id, "status": res.status, "amount_paise": res.amount_paise}


TOOL_MANIFEST = [
    {"name": "search_catalog", "money": False, "notes": "returns untrusted seller text"},
    {"name": "get_quote", "money": False, "notes": "merchant-signed price lock"},
    {"name": "propose_payment", "money": False, "notes": "returns a kernel verdict"},
    {"name": "execute_capability", "money": True, "notes": "requires a one-shot capability token"},
    {"name": "fulfil", "money": False, "notes": "seller post-condition check"},
    {"name": "mandate_state", "money": False, "notes": "read-only spend state"},
]
