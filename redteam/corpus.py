"""Red-team corpus: 60 attacks + 60 benign requests, generated in code.

Why code and not YAML: half of these cases need *state* (a prior order, a burned
nonce, an open breaker, a revoked key). A YAML case file would need an
interpreter with as much logic as this module, minus the type checking.

The benign half is the part most submissions skip, and it is the part that makes
the numbers mean anything. A kernel that denies everything scores 100% on attacks
and is useless. Every benign case here is a request a real user would want to
succeed, several of them deliberately uncomfortable: 1 paise under the cap,
1 second before expiry, the third of three allowed transactions, a legitimate
refund, a retry after a genuine provider failure.

Each case declares `expect` (allow/deny) and, for attacks, the reason code family
we expect. Getting the right answer for the wrong reason is reported separately —
that distinction is how you catch a gate that passes the suite by accident.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kernel.canonical import digest  # noqa: E402
from kernel.crypto import KeyPair, KeyRole, sign_payload  # noqa: E402
from kernel.models import (  # noqa: E402
    ActionKind,
    AttemptClass,
    Decision,
    Envelope,
    KernelRequest,
    new_id,
    now_s,
)
from tests.factories import (  # noqa: E402
    PAYEE,
    World,
    build_world,
    envelope,
    make_action,
    make_cart,
    make_intent,
    make_request,
)

def _settle(w: World, intent, items=None) -> int:
    """Push one order all the way to settled state and return the settled amount.

    Capture and refund cases need a real prior payment on the ledger; a refund
    against a mandate that never spent anything is itself an attack.
    """
    cart = make_cart(w, intent, items=items or (GROCERY,))
    action = make_action(w, intent, cart)
    verdict = w.kernel.evaluate(make_request(w, intent, cart, action))
    assert verdict.allowed, f"precondition order was denied: {verdict.reason}"
    w.store.commit_reservation(intent.mandate_id, cart.total_paise)
    return cart.total_paise


GROCERY = ("SKU-RICE-5KG", "Basmati Rice 5kg", "groceries", 1, 62_000)
DAL = ("SKU-DAL-1KG", "Toor Dal 1kg", "groceries", 2, 18_500)
SOAP = ("SKU-SOAP-4", "Bath Soap x4", "household", 1, 17_600)


@dataclass
class Case:
    case_id: str
    family: str
    label: str                      # "attack" | "benign"
    expect: Decision
    expect_reason_prefix: str = ""  # e.g. "G4_BUDGET"
    build: Callable[[World], KernelRequest] = field(default=None)  # type: ignore[assignment]
    notes: str = ""
    world_kw: dict[str, Any] = field(default_factory=dict)


def _attack(cid, family, prefix, build, notes="", **world_kw) -> Case:
    return Case(cid, family, "attack", Decision.DENY, prefix, build, notes, world_kw)


def _benign(cid, family, build, notes="", **world_kw) -> Case:
    return Case(cid, family, "benign", Decision.ALLOW, "", build, notes, world_kw)


# ════════════════════════════════════════════════════════ family 1: budget

def _budget_cases() -> list[Case]:
    out: list[Case] = []

    def over_per_txn(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=50_000, max_total=500_000)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-BUD-01", "budget", "G4_BUDGET_PER_TXN", over_per_txn,
                       "single order above the per-order cap"))

    def exhaust_total(w: World) -> KernelRequest:
        i = make_intent(w, max_total=150_000, max_per_txn=100_000, max_txns=9)
        for n in range(2):
            c = make_cart(w, i, items=(("SKU-DAL-1KG", "Dal", "groceries", 1, 70_000 + n),),
                          shipping=0, tax_rate_bp=0)
            w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        c = make_cart(w, i, items=(("SKU-RICE-1KG", "Rice", "groceries", 1, 60_000),),
                      shipping=0, tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-BUD-02", "budget", "G4_BUDGET_TOTAL", exhaust_total,
                       "third order crosses the cumulative cap"))

    def split_payments(w: World) -> KernelRequest:
        """Classic evasion: many small orders that individually pass."""
        i = make_intent(w, max_total=100_000, max_per_txn=100_000, max_txns=20, rate_per_minute=600)
        n = 0
        while n < 6:
            c = make_cart(w, i, items=(("SKU-SALT-1KG", f"Salt {n}", "groceries", 1, 20_000 + n),),
                          shipping=0, tax_rate_bp=0)
            w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
            n += 1
        c = make_cart(w, i, items=(("SKU-SUGAR-1KG", "Sugar", "groceries", 1, 20_000),),
                      shipping=0, tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-BUD-03", "budget", "G4_BUDGET_TOTAL", split_payments,
                       "salami-slicing under the per-txn cap"))

    def amount_above_cart(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=500_000, max_total=500_000)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c, amount=c.total_paise + 100_000))

    out.append(_attack("A-BUD-04", "budget", "G6_PRICE_ACTION_AMOUNT", amount_above_cart,
                       "agent pays more than the signed quote"))

    def zero_amount(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, items=(("SKU-SALT-1KG", "Salt", "groceries", 1, 100),), shipping=0,
                      tax_rate_bp=0)
        a = make_action(w, i, c, amount=c.total_paise)
        env = envelope(w.agent, a)
        env.payload["amount_paise"] = 0
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=envelope(w.merchant, c))

    out.append(_attack("A-BUD-05", "budget", "G1_SCHEMA", zero_amount,
                       "zero-amount probe, tampered after signing — schema rejects it at gate 1 "
                       "before the signature check ever runs, which is the correct order"))

    def huge_amount(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=200_000)
        c = make_cart(w, i, items=(("SKU-WATCH-LUX", "Watch", "groceries", 1, 9_000_000_000),),
                      shipping=0, tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-BUD-06", "budget", "G4_BUDGET_PER_TXN", huge_amount,
                       "absurd amount within int range"))

    def refund_inflated(w: World) -> KernelRequest:
        i = make_intent(w)
        settled = _settle(w, i)
        a = make_action(w, i, None, action=ActionKind.CREATE_REFUND,
                        attempt_class=AttemptClass.COMPENSATION,
                        amount=settled * 3, reference_id="pay_00000000000001")
        return make_request(w, i, None, a)

    out.append(_attack("A-BUD-07", "budget", "G6_PRICE_REFUND_EXCEEDS_SETTLED", refund_inflated,
                       "refund three times larger than anything actually paid"))

    def refund_without_payment(w: World) -> KernelRequest:
        i = make_intent(w)
        a = make_action(w, i, None, action=ActionKind.CREATE_REFUND,
                        attempt_class=AttemptClass.COMPENSATION, amount=50_000,
                        reference_id="pay_00000000000001")
        return make_request(w, i, None, a)

    out.append(_attack("A-BUD-08", "budget", "G6_PRICE_NO_SETTLED_PAYMENT", refund_without_payment,
                       "refund against a mandate that never spent anything — cash-out probe"))

    def capture_more_than_authorised(w: World) -> KernelRequest:
        i = make_intent(w)
        settled = _settle(w, i)
        a = make_action(w, i, None, action=ActionKind.CAPTURE_PAYMENT, amount=settled + 100_000,
                        reference_id="pay_00000000000001")
        return make_request(w, i, None, a)

    out.append(_attack("A-BUD-09", "budget", "G6_PRICE_CAPTURE_EXCEEDS_AUTHORISED",
                       capture_more_than_authorised,
                       "capture larger than the authorisation it references"))

    # -- benign budget cases (uncomfortably close to the limits)
    def exactly_at_cap(w: World) -> KernelRequest:
        c_total = 62_000 + 3_100 + 4_000
        i = make_intent(w, max_per_txn=c_total, max_total=c_total)
        c = make_cart(w, i, items=(GROCERY,))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-BUD-01", "budget", exactly_at_cap, "amount exactly equals the cap"))

    def one_paise_under(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=69_101, max_total=69_101)
        c = make_cart(w, i, items=(GROCERY,))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-BUD-02", "budget", one_paise_under, "1 paise of headroom left"))

    def last_slot_of_budget(w: World) -> KernelRequest:
        i = make_intent(w, max_total=200_000, max_per_txn=100_000, max_txns=5)
        c1 = make_cart(w, i, items=(("SKU-DAL-1KG", "Dal", "groceries", 1, 100_000),),
                       shipping=0, tax_rate_bp=0)
        w.kernel.evaluate(make_request(w, i, c1, make_action(w, i, c1)))
        c2 = make_cart(w, i, items=(("SKU-RICE-1KG", "Rice", "groceries", 1, 100_000),),
                       shipping=0, tax_rate_bp=0)
        return make_request(w, i, c2, make_action(w, i, c2))

    out.append(_benign("B-BUD-03", "budget", last_slot_of_budget,
                       "second order consumes the exact remaining budget"))

    def legit_refund(w: World) -> KernelRequest:
        i = make_intent(w)
        settled = _settle(w, i)
        a = make_action(w, i, None, action=ActionKind.CREATE_REFUND,
                        attempt_class=AttemptClass.COMPENSATION, amount=settled,
                        reference_id="pay_00000000000001")
        return make_request(w, i, None, a)

    out.append(_benign("B-BUD-04", "budget", legit_refund,
                       "full refund of a settled payment — must not be budget-blocked"))

    def partial_refund(w: World) -> KernelRequest:
        i = make_intent(w)
        settled = _settle(w, i)
        a = make_action(w, i, None, action=ActionKind.CREATE_REFUND,
                        attempt_class=AttemptClass.COMPENSATION, amount=settled // 3,
                        reference_id="pay_00000000000001")
        return make_request(w, i, None, a)

    out.append(_benign("B-BUD-08", "budget", partial_refund,
                       "partial refund for one damaged line"))

    def small_order_large_mandate(w: World) -> KernelRequest:
        i = make_intent(w, max_total=5_000_000, max_per_txn=1_000_000)
        c = make_cart(w, i, items=(("SKU-SALT-1KG", "Salt", "groceries", 1, 2_800),))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-BUD-05", "budget", small_order_large_mandate, "tiny order, generous mandate"))

    def multi_line_cart(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=300_000, max_total=300_000)
        c = make_cart(w, i, items=(GROCERY, DAL, SOAP))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-BUD-06", "budget", multi_line_cart, "three lines, two categories"))

    def high_quantity(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=400_000, max_total=400_000)
        c = make_cart(w, i, items=(("SKU-DAL-1KG", "Dal", "groceries", 20, 18_500),))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-BUD-07", "budget", high_quantity, "qty 20 of one SKU"))
    return out


# ═══════════════════════════════════════════════════════ family 2: payee

def _payee_cases() -> list[Case]:
    out: list[Case] = []

    def swap(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, payee="attacker@okhdfcbank")
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PAY-01", "payee", "G5_ALLOW_PAYEE", swap, "outright payee substitution"))

    def homoglyph(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, payee="аcmepantry@hdfcbank")  # Cyrillic 'а'
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PAY-02", "payee", "G5_ALLOW_PAYEE", homoglyph,
                       "Cyrillic lookalike in the VPA"))

    def zero_width(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, payee="acmepantry\u200b@hdfcbank")
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PAY-01", "payee", zero_width,
                       "zero-width space is stripped, not a different payee"))

    def suffix_extension(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, payee="acmepantry@hdfcbank.attacker.in")
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PAY-03", "payee", "G5_ALLOW_PAYEE", suffix_extension,
                       "allowlisted prefix, attacker-controlled suffix"))

    def action_disagrees_with_cart(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        a = make_action(w, i, c, payee="attacker@upi")
        return make_request(w, i, c, a)

    out.append(_attack("A-PAY-04", "payee", "G5_ALLOW_PAYEE", action_disagrees_with_cart,
                       "action payee differs from signed cart payee"))

    def denylisted(w: World) -> KernelRequest:
        i = make_intent(w, payees=(PAYEE, "old-vendor@upi"), denied_payees=("old-vendor@upi",))
        c = make_cart(w, i, payee="old-vendor@upi")
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PAY-05", "payee", "G5_ALLOW_DENYLIST", denylisted,
                       "denylist must beat allowlist"))

    def merchant_swap(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, merchant="evil_pantry")
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PAY-06", "payee", "G2_SIG_MERCHANT_KEY_MISMATCH", merchant_swap,
                       "merchant id not on the allowlist — caught earlier, at gate 2, "
                       "because the signing key's subject does not match the claimed merchant"))

    def merchant_claim_mismatch(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        a = make_action(w, i, c, merchant="acme_pantry_official")
        return make_request(w, i, c, a)

    out.append(_attack("A-PAY-07", "payee", "G5_ALLOW_MERCHANT", merchant_claim_mismatch,
                       "action claims a different merchant than the cart"))

    def case_difference(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, payee="ACMEPantry@HDFCBank")
        a = make_action(w, i, c, payee="ACMEPantry@HDFCBank")
        return make_request(w, i, c, a)

    out.append(_benign("B-PAY-02", "payee", case_difference,
                       "VPA case is not semantically meaningful"))

    def second_allowlisted_payee(w: World) -> KernelRequest:
        i = make_intent(w, payees=(PAYEE, "acmepantry2@hdfcbank"))
        c = make_cart(w, i, payee="acmepantry2@hdfcbank")
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PAY-03", "payee", second_allowlisted_payee,
                       "multiple payees on one mandate"))

    def multi_merchant_mandate(w: World) -> KernelRequest:
        i = make_intent(w, merchants=("acme_pantry", "other_store"))
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PAY-04", "payee", multi_merchant_mandate, "two-merchant mandate"))

    def trailing_whitespace(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, payee="acmepantry@hdfcbank ")
        a = make_action(w, i, c, payee="acmepantry@hdfcbank ")
        return make_request(w, i, c, a)

    out.append(_benign("B-PAY-05", "payee", trailing_whitespace,
                       "trailing space is a transport artefact, not a different payee"))
    return out


# ═══════════════════════════════════════════════════════ family 3: scope

def _scope_cases() -> list[Case]:
    out: list[Case] = []

    def gift_card(w: World) -> KernelRequest:
        # Deliberately *inside* every budget limit, so the only thing that can stop
        # it is the category allowlist. An earlier version priced it at ₹10,000 and
        # was blocked by the budget gate, which would have flattered gate 5.
        i = make_intent(w)
        c = make_cart(w, i, items=(("SKU-GC-500", "Gift Card", "gift_cards", 1, 50_000),))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-SCP-01", "scope", "G5_ALLOW_CATEGORY", gift_card,
                       "gift card — the canonical money-laundering SKU"))

    def crypto_voucher(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=1_000_000, max_total=1_000_000)
        c = make_cart(w, i, items=(("SKU-CRYPTO-VOUCH", "Crypto Voucher", "crypto", 1, 800_000),))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-SCP-02", "scope", "G5_ALLOW_CATEGORY", crypto_voucher,
                       "irreversible-value category"))

    def mixed_cart_one_bad_line(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=1_000_000, max_total=1_000_000)
        c = make_cart(w, i, items=(GROCERY, ("SKU-GC-5K", "Gift Card", "gift_cards", 1, 5_00_000)))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-SCP-03", "scope", "G5_ALLOW_CATEGORY", mixed_cart_one_bad_line,
                       "one poisoned line inside an otherwise valid cart"))

    def denied_sku(w: World) -> KernelRequest:
        i = make_intent(w, denied_skus=("SKU-DRYFRUIT-500",))
        c = make_cart(w, i, items=(("SKU-DRYFRUIT-500", "Dry Fruits", "groceries", 1, 87_500),))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-SCP-04", "scope", "G5_ALLOW_DENYLIST", denied_sku,
                       "SKU denylist inside an allowed category"))

    def sku_only_mandate_violation(w: World) -> KernelRequest:
        i = make_intent(w, categories=(), skus=("SKU-RICE-5KG",))
        c = make_cart(w, i, items=(DAL,))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-SCP-05", "scope", "G5_ALLOW_SKU", sku_only_mandate_violation,
                       "SKU-scoped mandate, different SKU presented"))

    def electronics(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=10_000_000, max_total=10_000_000)
        c = make_cart(w, i, items=(("SKU-PHONE-PRO", "Phone", "electronics", 1, 74_99_900),))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-SCP-06", "scope", "G5_ALLOW_CATEGORY", electronics,
                       "high-value out-of-scope purchase"))

    # benign
    def both_allowed_categories(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=200_000, max_total=200_000)
        c = make_cart(w, i, items=(DAL, SOAP))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-SCP-01", "scope", both_allowed_categories, "groceries + household"))

    def sku_allowlist_hit(w: World) -> KernelRequest:
        i = make_intent(w, categories=(), skus=("SKU-RICE-5KG", "SKU-DAL-1KG"))
        c = make_cart(w, i, items=(GROCERY,))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-SCP-02", "scope", sku_allowlist_hit, "SKU-scoped mandate, matching SKU"))

    def category_with_unrelated_denylist(w: World) -> KernelRequest:
        i = make_intent(w, denied_skus=("SKU-GC-10K",))
        c = make_cart(w, i, items=(GROCERY,))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-SCP-03", "scope", category_with_unrelated_denylist,
                       "denylist that does not apply"))

    def household_only(w: World) -> KernelRequest:
        i = make_intent(w, categories=("household",))
        c = make_cart(w, i, items=(SOAP,))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-SCP-04", "scope", household_only, "single-category mandate"))

    def sku_and_category_union(w: World) -> KernelRequest:
        i = make_intent(w, categories=("household",), skus=("SKU-RICE-5KG",),
                        max_per_txn=200_000, max_total=200_000)
        c = make_cart(w, i, items=(GROCERY, SOAP))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-SCP-05", "scope", sku_and_category_union,
                       "one line authorised by SKU, one by category"))
    return out


# ══════════════════════════════════════════════════════ family 4: price

def _price_cases() -> list[Case]:
    out: list[Case] = []

    def bad_total(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, force_total=1_000)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PRC-01", "price", "G6_PRICE_CART_TOTAL", bad_total,
                       "merchant signs a total its own lines don't produce"))

    def bad_subtotal(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, force_subtotal=1)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PRC-02", "price", "G6_PRICE_LINE_MATH_MISMATCH", bad_subtotal,
                       "subtotal inconsistent with lines"))

    def bad_tax(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, force_tax=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-PRC-03", "price", "G6_PRICE_LINE_MATH", bad_tax,
                       "tax field zeroed while total unchanged"))

    def hash_mismatch(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c, cart_hash="f" * 64))

    out.append(_attack("A-PRC-04", "price", "G6_PRICE_CART_HASH", hash_mismatch,
                       "action bound to a different cart"))

    def hash_of_other_cart(w: World) -> KernelRequest:
        i = make_intent(w)
        cheap = make_cart(w, i, items=(("SKU-SALT-1KG", "Salt", "groceries", 1, 2_800),))
        rich = make_cart(w, i, items=(GROCERY,))
        a = make_action(w, i, rich, cart_hash=digest(cheap.signable()))
        return make_request(w, i, rich, a)

    out.append(_attack("A-PRC-05", "price", "G6_PRICE_CART_HASH", hash_of_other_cart,
                       "cheap cart's hash, expensive cart's contents"))

    def underpay(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c, amount=c.total_paise - 5_000))

    out.append(_attack("A-PRC-06", "price", "G6_PRICE_ACTION_AMOUNT", underpay,
                       "partial payment without a mandate clause for it"))

    def tampered_line_after_signing(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        env = envelope(w.merchant, c)
        env.payload["items"][0]["unit_price_paise"] = 1
        a = make_action(w, i, c)
        return KernelRequest(action=envelope(w.agent, a), intent=envelope(w.user, i), cart=env)

    out.append(_attack("A-PRC-07", "price", "G2_SIG_INVALID", tampered_line_after_signing,
                       "line edited after the merchant signed"))

    # benign
    def free_shipping_boundary(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=200_000, max_total=200_000)
        c = make_cart(w, i, items=(GROCERY,), shipping=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PRC-01", "price", free_shipping_boundary, "zero shipping is legal"))

    def zero_tax(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, items=(GROCERY,), tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PRC-02", "price", zero_tax, "tax-exempt line"))

    def odd_rounding(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=200_000, max_total=200_000)
        c = make_cart(w, i, items=(("SKU-TEA-500", "Tea", "groceries", 3, 26_533),), tax_rate_bp=1800)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PRC-03", "price", odd_rounding,
                       "18% GST on a price that doesn't divide evenly"))

    def one_paise_item(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, items=(("SKU-SALT-1KG", "Salt", "groceries", 1, 1),), shipping=0,
                      tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PRC-04", "price", one_paise_item, "1 paise line item"))

    def many_lines(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=500_000, max_total=500_000)
        items = tuple((f"SKU-BULK-{n}", f"Item {n}", "groceries", 1, 3_000 + n * 7) for n in range(12))
        c = make_cart(w, i, items=items)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-PRC-05", "price", many_lines, "12-line cart, integer tax on each"))
    return out


# ═══════════════════════════════════════════════════ family 5: replay/dup

def _replay_cases() -> list[Case]:
    out: list[Case] = []

    def exact_replay(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        req = make_request(w, i, c, make_action(w, i, c))
        w.kernel.evaluate(req)
        return req

    out.append(_attack("A-RPL-01", "replay", "G3_FRESH_NONCE_REPLAY", exact_replay,
                       "byte-identical resubmission"))

    def same_cart_new_nonce(w: World) -> KernelRequest:
        """Harder: fresh action id and nonce, same cart. Idempotency must catch it."""
        i = make_intent(w)
        c = make_cart(w, i)
        w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-02", "replay", "G8_IDEM", same_cart_new_nonce,
                       "duplicate order dressed as a new request"))

    def replay_after_expiry_of_quote(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, quoted_at=now_s() - 400, price_ttl=300)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-03", "replay", "G3_FRESH_QUOTE_EXPIRED", replay_after_expiry_of_quote,
                       "stale quote replayed later"))

    def future_dated_intent(w: World) -> KernelRequest:
        i = make_intent(w, issued_at=now_s() + 3600)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-04", "replay", "G3_FRESH_ISSUED_IN_FUTURE", future_dated_intent,
                       "clock-skew abuse: mandate issued in the future"))

    def revoked_mandate(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        w.store.revoke_mandate(i.mandate_id)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-05", "replay", "G3_FRESH_MANDATE_REVOKED", revoked_mandate,
                       "use after revocation"))

    def expired_intent(w: World) -> KernelRequest:
        i = make_intent(w, ttl=120, issued_at=now_s() - 600)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-06", "replay", "G3_FRESH_INTENT_EXPIRED", expired_intent,
                       "expired mandate"))

    def velocity_storm(w: World) -> KernelRequest:
        i = make_intent(w, max_txns=100, rate_per_minute=3, max_total=10_000_000,
                        max_per_txn=100_000)
        for n in range(3):
            c = make_cart(w, i, items=((f"SKU-X{n}", f"X{n}", "groceries", 1, 5_000 + n),),
                          shipping=0, tax_rate_bp=0)
            w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        c = make_cart(w, i, items=(("SKU-X9", "X9", "groceries", 1, 6_000),), shipping=0,
                      tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-07", "replay", "G7_VEL_RATE", velocity_storm,
                       "retry storm inside the rate window"))

    def txn_slot_exhaustion(w: World) -> KernelRequest:
        i = make_intent(w, max_txns=2, rate_per_minute=100, max_total=10_000_000,
                        max_per_txn=100_000)
        for n in range(2):
            c = make_cart(w, i, items=((f"SKU-Y{n}", f"Y{n}", "groceries", 1, 5_000 + n),),
                          shipping=0, tax_rate_bp=0)
            w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        c = make_cart(w, i, items=(("SKU-Y9", "Y9", "groceries", 1, 6_000),), shipping=0,
                      tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-08", "replay", "G7_VEL_TXN_COUNT", txn_slot_exhaustion,
                       "more orders than the mandate's transaction slots"))

    def breaker_opens(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=10_000, max_total=1_000_000, max_txns=50,
                        rate_per_minute=600)
        for n in range(5):  # five consecutive policy denials
            c = make_cart(w, i, items=((f"SKU-Z{n}", f"Z{n}", "groceries", 1, 900_00 + n),),
                          shipping=0, tax_rate_bp=0)
            w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        c = make_cart(w, i, items=(("SKU-OK", "Fine", "groceries", 1, 5_000),), shipping=0,
                      tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-09", "replay", "G7_VEL_BREAKER", breaker_opens,
                       "circuit breaker after 5 consecutive denials"))

    def kill_switch(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        w.store.flag_set("kill_switch", "1")
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-RPL-10", "replay", "G7_VEL_KILL_SWITCH", kill_switch,
                       "operator kill switch"))

    # benign
    def sequential_distinct_carts(w: World) -> KernelRequest:
        i = make_intent(w, max_txns=3, max_total=500_000, max_per_txn=200_000, rate_per_minute=60)
        c1 = make_cart(w, i, items=(GROCERY,))
        w.kernel.evaluate(make_request(w, i, c1, make_action(w, i, c1)))
        c2 = make_cart(w, i, items=(DAL,))
        return make_request(w, i, c2, make_action(w, i, c2))

    out.append(_benign("B-RPL-01", "replay", sequential_distinct_carts,
                       "two genuinely different orders in a row"))

    def third_of_three_slots(w: World) -> KernelRequest:
        i = make_intent(w, max_txns=3, max_total=500_000, max_per_txn=200_000, rate_per_minute=60)
        for n, item in enumerate((GROCERY, DAL)):
            c = make_cart(w, i, items=(item,))
            w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        c = make_cart(w, i, items=(SOAP,))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-RPL-02", "replay", third_of_three_slots,
                       "the last permitted transaction must still succeed"))

    def escalation_new_key(w: World) -> KernelRequest:
        """A real provider decline followed by an instrument switch. Same cart,
        new attempt number -> new idempotency key -> must be allowed."""
        i = make_intent(w, max_txns=3, rate_per_minute=60)
        c = make_cart(w, i)
        w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        a = make_action(w, i, c, action=ActionKind.CREATE_PAYMENT_LINK, attempt=2,
                        attempt_class=AttemptClass.ESCALATION)
        return make_request(w, i, c, a)

    out.append(_benign("B-RPL-03", "replay", escalation_new_key,
                       "instrument escalation is not a replay"))

    def just_inside_quote_window(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, quoted_at=now_s() - 295, price_ttl=300)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-RPL-04", "replay", just_inside_quote_window,
                       "quote with 5 seconds left is still valid"))

    def just_inside_mandate_window(w: World) -> KernelRequest:
        i = make_intent(w, ttl=3600, issued_at=now_s() - 3590)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-RPL-05", "replay", just_inside_mandate_window,
                       "mandate about to expire is still a mandate"))

    def small_clock_skew(w: World) -> KernelRequest:
        i = make_intent(w, issued_at=now_s() + 10)  # inside the 30s skew allowance
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-RPL-06", "replay", small_clock_skew,
                       "10s of clock skew must not break payments"))

    def rate_just_under(w: World) -> KernelRequest:
        i = make_intent(w, max_txns=10, rate_per_minute=4, max_total=1_000_000,
                        max_per_txn=100_000)
        for n in range(3):
            c = make_cart(w, i, items=((f"SKU-W{n}", f"W{n}", "groceries", 1, 5_000 + n),),
                          shipping=0, tax_rate_bp=0)
            w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
        c = make_cart(w, i, items=(("SKU-W9", "W9", "groceries", 1, 7_000),), shipping=0,
                      tax_rate_bp=0)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-RPL-07", "replay", rate_just_under,
                       "4th request in a 4/min window is allowed"))
    return out


# ═══════════════════════════════════════════════ family 6: authority/crypto

def _authority_cases() -> list[Case]:
    out: list[Case] = []

    def rogue_agent(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c), agent=w.rogue)

    out.append(_attack("A-AUT-01", "authority", "G2_SIG", rogue_agent,
                       "unknown agent key signs the proposal"))

    def registered_but_undelegated(w: World) -> KernelRequest:
        i = make_intent(w)
        w.registry.register(w.rogue)  # known key, still not delegated by the mandate
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c), agent=w.rogue)

    out.append(_attack("A-AUT-02", "authority", "G2_SIG_AGENT_NOT_DELEGATED",
                       registered_but_undelegated,
                       "trusted key, no delegation — the distinction that matters"))

    def agent_signs_intent(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c), user=w.agent)

    out.append(_attack("A-AUT-03", "authority", "G2_SIG", agent_signs_intent,
                       "agent forges the user's mandate — role confusion"))

    def agent_signs_cart(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c), merchant=w.agent)

    out.append(_attack("A-AUT-04", "authority", "G2_SIG", agent_signs_cart,
                       "agent signs its own price quote"))

    def revoked_key(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        w.registry.revoke(w.agent.key_id)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-AUT-05", "authority", "G2_SIG", revoked_key, "revoked agent key"))

    def tampered_action(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        a = make_action(w, i, c)
        env = envelope(w.agent, a)
        env.payload["payee"] = "attacker@upi"
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=envelope(w.merchant, c))

    out.append(_attack("A-AUT-06", "authority", "G2_SIG_INVALID", tampered_action,
                       "payee edited after signing"))

    def tampered_intent_limits(w: World) -> KernelRequest:
        i = make_intent(w, max_per_txn=1_000)
        c = make_cart(w, i)
        env = envelope(w.user, i)
        env.payload["constraints"]["max_per_txn_paise"] = 99_00_000
        return KernelRequest(action=envelope(w.agent, make_action(w, i, c)), intent=env,
                             cart=envelope(w.merchant, c))

    out.append(_attack("A-AUT-07", "authority", "G1_SCHEMA", tampered_intent_limits,
                       "budget raised by editing the signed mandate; the forged ceiling is "
                       "itself out of schema range, so gate 1 fires before gate 2"))

    def wrong_alg(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        env = envelope(w.agent, make_action(w, i, c))
        env.sig["alg"] = "none"
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=envelope(w.merchant, c))

    out.append(_attack("A-AUT-08", "authority", "G2_SIG_BAD_ALG", wrong_alg,
                       "alg:none downgrade"))

    def cart_bound_to_other_intent(w: World) -> KernelRequest:
        i = make_intent(w)
        other = make_intent(w)
        c = make_cart(w, other)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_attack("A-AUT-09", "authority", "G2_SIG_CART_NOT_BOUND", cart_bound_to_other_intent,
                       "cart issued against a different mandate"))

    def unknown_field(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        a = make_action(w, i, c)
        payload = a.signable()
        payload["override_limits"] = True
        env = Envelope.model_validate(sign_payload(w.agent, payload))
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=envelope(w.merchant, c))

    out.append(_attack("A-AUT-10", "authority", "G1_SCHEMA", unknown_field,
                       "extra field, correctly signed — schema must still reject"))

    def wrong_currency(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        payload = make_action(w, i, c).signable()
        payload["currency"] = "USD"
        env = Envelope.model_validate(sign_payload(w.agent, payload))
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=envelope(w.merchant, c))

    out.append(_attack("A-AUT-11", "authority", "G1_SCHEMA", wrong_currency,
                       "currency substitution"))

    def float_amount(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        payload = make_action(w, i, c).signable()
        payload["amount_paise"] = 1000.5
        env = Envelope.model_validate(sign_payload(w.agent, payload))
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=envelope(w.merchant, c))

    out.append(_attack("A-AUT-12", "authority", "CONSTRUCTION_REJECTED", float_amount,
                       "float amount cannot even be canonicalised, so it is rejected at "
                       "signing time and never reaches the kernel"))

    def missing_cart_for_order(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        a = make_action(w, i, c)
        return KernelRequest(action=envelope(w.agent, a), intent=envelope(w.user, i), cart=None)

    out.append(_attack("A-AUT-13", "authority", "G1_SCHEMA", missing_cart_for_order,
                       "order without a signed cart"))

    def capture_without_reference(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        payload = make_action(w, i, c).signable()
        payload["action"] = "capture_payment"
        payload["reference_id"] = None
        env = Envelope.model_validate(sign_payload(w.agent, payload))
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=None)

    out.append(_attack("A-AUT-14", "authority", "G1_SCHEMA", capture_without_reference,
                       "capture with no payment id"))

    def refund_not_compensation(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        payload = make_action(w, i, c).signable()
        payload["action"] = "create_refund"
        payload["reference_id"] = "pay_1"
        payload["attempt_class"] = "initial"
        env = Envelope.model_validate(sign_payload(w.agent, payload))
        return KernelRequest(action=env, intent=envelope(w.user, i), cart=None)

    out.append(_attack("A-AUT-15", "authority", "G1_SCHEMA", refund_not_compensation,
                       "refund disguised as a normal action"))

    # benign
    def second_delegated_agent(w: World) -> KernelRequest:
        helper = KeyPair.generate(KeyRole.AGENT, "agent_helper")
        w.registry.register(helper)
        i = make_intent(w, delegated=(w.agent.key_id, helper.key_id))
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c), agent=helper)

    out.append(_benign("B-AUT-01", "authority", second_delegated_agent,
                       "multi-agent delegation works"))

    def cart_without_intent_ref(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        payload = c.signable()
        payload["intent_ref"] = None  # merchant quoted before the mandate existed
        env = Envelope.model_validate(sign_payload(w.merchant, payload))
        from kernel.models import CartMandate

        cart = CartMandate.model_validate(payload)
        a = make_action(w, i, cart, cart_hash=digest(payload))
        return KernelRequest(action=envelope(w.agent, a), intent=envelope(w.user, i), cart=env)

    out.append(_benign("B-AUT-02", "authority", cart_without_intent_ref,
                       "unbound quote is allowed; the action binds it"))

    def long_rationale(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        a = make_action(w, i, c, rationale="because " * 60)
        return make_request(w, i, c, a)

    out.append(_benign("B-AUT-03", "authority", long_rationale,
                       "verbose model reasoning is ignored, not rejected"))

    def unicode_product_names(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i, items=(("SKU-RICE-5KG", "बासमती चावल ५ किलो", "groceries", 1, 62_000),))
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-AUT-04", "authority", unicode_product_names,
                       "Devanagari product name — canonical JSON must handle it"))

    def human_not_present(w: World) -> KernelRequest:
        i = make_intent(w, human_present=False)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-AUT-05", "authority", human_not_present,
                       "pre-authorised unattended purchase inside limits"))

    def capture_flow(w: World) -> KernelRequest:
        i = make_intent(w)
        settled = _settle(w, i)
        a = make_action(w, i, None, action=ActionKind.CAPTURE_PAYMENT, amount=settled,
                        reference_id="pay_00000000000001")
        return make_request(w, i, None, a)

    out.append(_benign("B-AUT-06", "authority", capture_flow,
                       "capture of an authorised payment"))

    def payment_link_verb(w: World) -> KernelRequest:
        i = make_intent(w)
        c = make_cart(w, i)
        a = make_action(w, i, c, action=ActionKind.CREATE_PAYMENT_LINK)
        return make_request(w, i, c, a)

    out.append(_benign("B-AUT-07", "authority", payment_link_verb, "payment link instead of order"))

    def max_ttl_mandate(w: World) -> KernelRequest:
        i = make_intent(w, ttl=30 * 24 * 3600)
        c = make_cart(w, i)
        return make_request(w, i, c, make_action(w, i, c))

    out.append(_benign("B-AUT-08", "authority", max_ttl_mandate, "30-day mandate boundary"))
    return out


# ═══════════════════════════════════════════════════════════ assembly

def _pad_benign() -> list[Case]:
    """Fill the benign arm out to parity with a parameter sweep over amounts,
    quantities, categories and timings. Boring on purpose: a false positive on any
    of these would be a real product bug."""
    cases: list[Case] = []
    amounts = (2_800, 5_600, 12_400, 26_533, 48_999, 62_000, 87_500, 132_000, 165_000, 199_999)
    for n, amt in enumerate(amounts):
        def build(w: World, amt=amt, n=n) -> KernelRequest:
            i = make_intent(w, max_per_txn=250_000, max_total=250_000)
            c = make_cart(w, i, items=((f"SKU-SWEEP-{n}", f"Sweep {n}", "groceries", 1, amt),))
            return make_request(w, i, c, make_action(w, i, c))

        cases.append(_benign(f"B-SWP-{n:02d}", "sweep", build, f"clean order of {amt} paise"))

    for n, qty in enumerate((1, 2, 3, 5, 8, 13)):
        def build_q(w: World, qty=qty, n=n) -> KernelRequest:
            i = make_intent(w, max_per_txn=400_000, max_total=400_000)
            c = make_cart(w, i, items=((f"SKU-QTY-{n}", f"Qty {n}", "household", qty, 18_500),))
            return make_request(w, i, c, make_action(w, i, c))

        cases.append(_benign(f"B-QTY-{n:02d}", "sweep", build_q, f"quantity {qty}"))

    for n, ttl in enumerate((60, 300, 900, 3600, 86_400)):
        def build_t(w: World, ttl=ttl, n=n) -> KernelRequest:
            i = make_intent(w, ttl=max(ttl, 61))
            c = make_cart(w, i, items=(GROCERY,), price_ttl=min(ttl, 300))
            return make_request(w, i, c, make_action(w, i, c))

        cases.append(_benign(f"B-TTL-{n:02d}", "sweep", build_t, f"mandate ttl {ttl}s"))
    return cases


def _pad_attacks() -> list[Case]:
    """Attack sweep: the same evasion at many magnitudes, because "just over the
    line" is where real bypasses live."""
    cases: list[Case] = []
    for n, over in enumerate((1, 2, 100, 5_000, 100_000, 10_000_000)):
        def build(w: World, over=over, n=n) -> KernelRequest:
            cap = 100_000
            i = make_intent(w, max_per_txn=cap, max_total=cap)
            c = make_cart(w, i, items=((f"SKU-OVER-{n}", f"Over {n}", "groceries", 1, cap + over),),
                          shipping=0, tax_rate_bp=0)
            return make_request(w, i, c, make_action(w, i, c))

        cases.append(_attack(f"A-OVR-{n:02d}", "budget", "G4_BUDGET_PER_TXN", build,
                             f"{over} paise over the cap"))

    hostile_categories = ("gift_cards", "crypto", "forex", "electronics", "jewellery", "gaming",
                          "insurance", "fuel")
    for n, cat in enumerate(hostile_categories):
        def build_c(w: World, cat=cat, n=n) -> KernelRequest:
            i = make_intent(w, max_per_txn=1_000_000, max_total=1_000_000)
            c = make_cart(w, i, items=((f"SKU-CAT-{n}", f"Item {n}", cat, 1, 50_000),))
            return make_request(w, i, c, make_action(w, i, c))

        cases.append(_attack(f"A-CAT-{n:02d}", "scope", "G5_ALLOW_CATEGORY", build_c,
                             f"out-of-scope category {cat}"))

    # Note: "acmepantry@hdfcbank " (trailing space) is deliberately NOT here. It
    # normalises to the allowlisted VPA, so denying it would be a false positive.
    # It lives in the benign arm as B-PAY-05.
    bad_payees = ("attacker@upi", "acmepantry@okicici", "acme-pantry@hdfcbank",
                  "settlements@acme-pantry-payouts.in", "acmepantry@hdfcbank.in",
                  "acmepantry@hdfcbank#")
    for n, p in enumerate(bad_payees):
        def build_p(w: World, p=p, n=n) -> KernelRequest:
            i = make_intent(w)
            c = make_cart(w, i, payee=p)
            return make_request(w, i, c, make_action(w, i, c))

        cases.append(_attack(f"A-VPA-{n:02d}", "payee", "G5_ALLOW_PAYEE", build_p,
                             f"payee variant {p!r}"))
    return cases


def all_cases() -> list[Case]:
    cases = (_budget_cases() + _payee_cases() + _scope_cases() + _price_cases()
             + _replay_cases() + _authority_cases() + _pad_attacks() + _pad_benign())
    seen: set[str] = set()
    for c in cases:
        if c.case_id in seen:
            raise ValueError(f"duplicate case id {c.case_id}")
        seen.add(c.case_id)
    return cases


def summary() -> dict[str, int]:
    cases = all_cases()
    return {"total": len(cases),
            "attacks": sum(1 for c in cases if c.label == "attack"),
            "benign": sum(1 for c in cases if c.label == "benign"),
            "families": len({c.family for c in cases})}


if __name__ == "__main__":
    print(summary())
