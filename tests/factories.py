"""Test/demo world builder. Also imported by the red-team runner and the seed
script, so there is exactly one definition of "a valid request" in the repo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kernel.canonical import digest
from kernel.config import KernelConfig
from kernel.crypto import KeyPair, KeyRegistry, KeyRole, sign_payload
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
    new_id,
    now_s,
)
from kernel.pipeline import Kernel
from kernel.store import Store

MERCHANT = "acme_pantry"
PAYEE = "acmepantry@hdfcbank"


@dataclass
class World:
    store: Store
    registry: KeyRegistry
    kernel: Kernel
    user: KeyPair
    agent: KeyPair
    merchant: KeyPair
    cfg: KernelConfig
    rogue: KeyPair = field(default=None)  # type: ignore[assignment]


def build_world(db_path: str = ":memory:", **cfg_kw: Any) -> World:
    cfg = KernelConfig(db_path=db_path, **cfg_kw)
    store = Store(db_path)
    registry = KeyRegistry()
    user = KeyPair.generate(KeyRole.USER, "user_nikitha")
    agent = KeyPair.generate(KeyRole.AGENT, "agent_pantry_bot")
    merchant = KeyPair.generate(KeyRole.MERCHANT, MERCHANT)
    rogue = KeyPair.generate(KeyRole.AGENT, "agent_rogue")
    for kp in (user, agent, merchant):
        registry.register(kp)
    return World(store=store, registry=registry, kernel=Kernel(store, registry, cfg),
                 user=user, agent=agent, merchant=merchant, cfg=cfg, rogue=rogue)


__all__ = [
    "World", "build_world", "make_intent", "make_cart", "make_action", "envelope",
    "make_request", "happy_path", "MERCHANT", "PAYEE", "KeyPair", "KeyRole", "digest",
    "ActionKind", "AttemptClass", "now_s", "new_id", "Store", "Kernel", "KernelConfig",
    "KernelRequest", "Envelope", "CartItem", "CartMandate", "Constraints",
    "IntentMandate", "ProposedAction",
]


def make_intent(w: World, *, max_total=500_000, max_per_txn=200_000, max_txns=3,
                categories=("groceries", "household"), skus=(), merchants=(MERCHANT,),
                payees=(PAYEE,), denied_skus=(), denied_payees=(), ttl=3600,
                human_present=True, rate_per_minute=6, subject="user_nikitha",
                delegated: tuple[str, ...] | None = None,
                playback="Buy up to ₹5,000 of groceries from Acme Pantry, max ₹2,000 per order.",
                issued_at: int | None = None) -> IntentMandate:
    t = issued_at if issued_at is not None else now_s()
    return IntentMandate(
        mandate_id=new_id("mnd"), subject=subject,
        delegated_agents=delegated if delegated is not None else (w.agent.key_id,),
        human_present=human_present, prompt_playback=playback,
        constraints=Constraints(
            max_total_paise=max_total, max_per_txn_paise=max_per_txn, max_transactions=max_txns,
            rate_per_minute=rate_per_minute, allowed_merchants=merchants, allowed_payees=payees,
            allowed_skus=skus, allowed_categories=categories,
            denied_skus=denied_skus, denied_payees=denied_payees),
        issued_at=t, expires_at=t + ttl, nonce=new_id("n"))


def make_cart(w: World, intent: IntentMandate | None = None, *,
              items: tuple[tuple[str, str, str, int, int], ...] = (
                  ("SKU-RICE-5KG", "Basmati Rice 5kg", "groceries", 1, 62_000),
                  ("SKU-DAL-1KG", "Toor Dal 1kg", "groceries", 2, 18_500),
              ),
              shipping=4_000, tax_rate_bp=500, merchant=MERCHANT, payee=PAYEE,
              price_ttl=300, quoted_at: int | None = None,
              force_total: int | None = None, force_subtotal: int | None = None,
              force_tax: int | None = None) -> CartMandate:
    lines = []
    subtotal = 0
    tax = 0
    for sku, name, cat, qty, unit in items:
        line = unit * qty
        line_tax = line * tax_rate_bp // 10_000
        subtotal += line
        tax += line_tax
        lines.append(CartItem(sku=sku, name=name, category=cat, qty=qty,
                              unit_price_paise=unit, tax_paise=line_tax))
    t = quoted_at if quoted_at is not None else now_s()
    total = subtotal + tax + shipping
    return CartMandate(
        cart_id=new_id("cart"), merchant_id=merchant,
        intent_ref=intent.mandate_id if intent else None,
        items=tuple(lines),
        subtotal_paise=force_subtotal if force_subtotal is not None else subtotal,
        tax_paise=force_tax if force_tax is not None else tax,
        shipping_paise=shipping,
        total_paise=force_total if force_total is not None else total,
        payee=payee, quoted_at=t, price_valid_until=t + price_ttl, nonce=new_id("n"))


def make_action(w: World, intent: IntentMandate, cart: CartMandate | None = None, *,
                action=ActionKind.CREATE_ORDER, amount: int | None = None,
                attempt=1, attempt_class=AttemptClass.INITIAL, merchant: str | None = None,
                payee: str | None = None, cart_hash: str | None = None,
                reference_id: str | None = None, rationale="", action_id: str | None = None,
                client_nonce: str | None = None) -> ProposedAction:
    return ProposedAction(
        action_id=action_id or new_id("act"), action=action,
        amount_paise=amount if amount is not None else (cart.total_paise if cart else 1000),
        merchant_id=merchant or (cart.merchant_id if cart else MERCHANT),
        payee=payee or (cart.payee if cart else PAYEE),
        intent_ref=intent.mandate_id, cart_ref=cart.cart_id if cart else "none",
        cart_hash=cart_hash if cart_hash is not None else (digest(cart.signable()) if cart else "-"),
        attempt=attempt, attempt_class=attempt_class, client_nonce=client_nonce or new_id("cn"),
        rationale=rationale, reference_id=reference_id)


def envelope(kp: KeyPair, obj: Any) -> Envelope:
    payload = obj.signable() if hasattr(obj, "signable") else obj
    return Envelope.model_validate(sign_payload(kp, payload))


def make_request(w: World, intent: IntentMandate, cart: CartMandate | None,
                 action: ProposedAction, *, agent: KeyPair | None = None,
                 user: KeyPair | None = None, merchant: KeyPair | None = None) -> KernelRequest:
    return KernelRequest(
        action=envelope(agent or w.agent, action),
        intent=envelope(user or w.user, intent),
        cart=envelope(merchant or w.merchant, cart) if cart is not None else None,
    )


def happy_path(w: World, **intent_kw: Any):
    intent = make_intent(w, **intent_kw)
    cart = make_cart(w, intent)
    action = make_action(w, intent, cart)
    return intent, cart, action, make_request(w, intent, cart, action)
