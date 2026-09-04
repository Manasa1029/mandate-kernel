"""Gate-level behaviour, including the properties that are easy to get wrong:
gate ordering, fail-closed defaults, non-punitive denials, and the fact that an
allow is never a side-effect-free read."""
from __future__ import annotations

import pytest

from kernel.errors import Reason
from kernel.gates import GATE_NAMES, PIPELINE
from kernel.models import ActionKind, AttemptClass, Decision, now_s
from kernel.canonical import digest
from tests.factories import (
    PAYEE,
    build_world,
    envelope,
    happy_path,
    make_action,
    make_cart,
    make_intent,
    make_request,
)


def test_pipeline_order_is_fixed():
    assert GATE_NAMES == ("schema", "signature", "freshness", "budget",
                          "allowlist", "price_binding", "velocity", "idempotency")
    assert len(PIPELINE) == 8


def test_happy_path_passes_every_gate(world):
    _, _, _, req = happy_path(world)
    v = world.kernel.evaluate(req)
    assert v.allowed, v.reason
    assert [g.decision for g in v.gates] == [Decision.ALLOW] * 8
    assert v.capability is not None and v.capability.idempotency_key


def test_verdict_records_every_gate_even_on_deny(world):
    i = make_intent(world, max_per_txn=1)
    c = make_cart(world, i)
    v = world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    assert not v.allowed
    # Gates after the failure are not evaluated, but the ones that ran are all recorded
    # and the failing one is the last entry. Short-circuiting is deliberate: gate 8
    # takes a lock, so we must not reach it once the answer is already no.
    assert v.gates[-1].decision is Decision.DENY
    assert v.gates[-1].ordinal == 4


def test_denial_is_explainable(world):
    i = make_intent(world, max_per_txn=1)
    c = make_cart(world, i)
    v = world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    failing = v.gates[-1]
    assert failing.reason == Reason.BUDGET_PER_TXN_EXCEEDED
    assert failing.detail and failing.evidence  # a human can read why


def test_capability_is_redacted_in_the_ledger(world):
    _, _, _, req = happy_path(world)
    v = world.kernel.evaluate(req)
    blob = str(world.store.recent(10))
    assert v.capability is not None
    assert v.capability.token not in blob


# ───────────────────────────────────────────────────── gate 1: schema

def test_unknown_field_is_fatal(world):
    from kernel.crypto import sign_payload
    from kernel.models import Envelope, KernelRequest

    i = make_intent(world)
    c = make_cart(world, i)
    payload = make_action(world, i, c).signable()
    payload["skip_limits"] = True
    env = Envelope.model_validate(sign_payload(world.agent, payload))
    v = world.kernel.evaluate(KernelRequest(action=env, intent=envelope(world.user, i),
                                            cart=envelope(world.merchant, c)))
    assert not v.allowed and v.reason.startswith("G1_SCHEMA")


def test_string_amount_is_not_coerced(world):
    from kernel.crypto import sign_payload
    from kernel.models import Envelope, KernelRequest

    i = make_intent(world)
    c = make_cart(world, i)
    payload = make_action(world, i, c).signable()
    payload["amount_paise"] = str(payload["amount_paise"])
    env = Envelope.model_validate(sign_payload(world.agent, payload))
    v = world.kernel.evaluate(KernelRequest(action=env, intent=envelope(world.user, i),
                                            cart=envelope(world.merchant, c)))
    assert not v.allowed and v.reason.startswith("G1_SCHEMA")


def test_order_without_cart_is_rejected(world):
    from kernel.models import KernelRequest

    i = make_intent(world)
    c = make_cart(world, i)
    a = make_action(world, i, c)
    v = world.kernel.evaluate(KernelRequest(action=envelope(world.agent, a),
                                            intent=envelope(world.user, i), cart=None))
    assert not v.allowed


# ─────────────────────────────────────────────── gate 2: signature/authority

def test_undelegated_agent_is_denied(world):
    world.registry.register(world.rogue)
    i = make_intent(world)
    c = make_cart(world, i)
    v = world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c),
                                           agent=world.rogue))
    assert v.reason == Reason.SIG_AGENT_NOT_DELEGATED


def test_unknown_agent_key_is_denied(world):
    i = make_intent(world)
    c = make_cart(world, i)
    v = world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c),
                                           agent=world.rogue))
    assert v.reason.startswith("G2_SIG")


# ───────────────────────────────────────────────────── gate 3: freshness

@pytest.mark.parametrize("skew,expected_allow", [(0, True), (25, True), (120, False)])
def test_clock_skew_tolerance(skew, expected_allow):
    w = build_world()
    i = make_intent(w, issued_at=now_s() + skew)
    c = make_cart(w, i)
    v = w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
    assert v.allowed is expected_allow, v.reason


def test_revocation_blocks_new_spend_but_not_refunds(world):
    i = make_intent(world)
    c = make_cart(world, i)
    world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    world.store.commit_reservation(i.mandate_id, c.total_paise)
    world.store.revoke_mandate(i.mandate_id)

    c2 = make_cart(world, i)
    blocked = world.kernel.evaluate(make_request(world, i, c2, make_action(world, i, c2)))
    assert blocked.reason == Reason.FRESH_MANDATE_REVOKED

    refund = make_action(world, i, None, action=ActionKind.CREATE_REFUND,
                         attempt_class=AttemptClass.COMPENSATION, amount=c.total_paise,
                         reference_id="pay_1")
    allowed = world.kernel.evaluate(make_request(world, i, None, refund))
    assert allowed.allowed, allowed.reason


def test_nonce_replay_is_denied(world):
    _, _, _, req = happy_path(world)
    assert world.kernel.evaluate(req).allowed
    assert world.kernel.evaluate(req).reason == Reason.FRESH_NONCE_REPLAY


# ─────────────────────────────────────────────────────── gate 4: budget

def test_reservation_is_taken_on_allow_not_on_deny(world):
    i = make_intent(world)
    c = make_cart(world, i)
    world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    assert world.store.spend_state(i.mandate_id)["reserved"] == c.total_paise

    i2 = make_intent(world, max_per_txn=1)
    c2 = make_cart(world, i2)
    world.kernel.evaluate(make_request(world, i2, c2, make_action(world, i2, c2)))
    assert world.store.spend_state(i2.mandate_id)["reserved"] == 0


def test_refund_restores_headroom(world):
    i = make_intent(world, max_total=200_000, max_per_txn=200_000, max_txns=5)
    c = make_cart(world, i, items=(("SKU-A", "A", "groceries", 1, 190_000),), shipping=0,
                  tax_rate_bp=0)
    assert world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c))).allowed
    world.store.commit_reservation(i.mandate_id, c.total_paise)

    c2 = make_cart(world, i, items=(("SKU-B", "B", "groceries", 1, 50_000),), shipping=0,
                   tax_rate_bp=0)
    assert not world.kernel.evaluate(make_request(world, i, c2, make_action(world, i, c2))).allowed

    world.store.credit_refund(i.mandate_id, c.total_paise)
    c3 = make_cart(world, i, items=(("SKU-C", "C", "groceries", 1, 50_000),), shipping=0,
                   tax_rate_bp=0)
    assert world.kernel.evaluate(make_request(world, i, c3, make_action(world, i, c3))).allowed


# ──────────────────────────────────────────────────── gate 5: allowlist

def test_payee_normalisation_matches_but_does_not_over_match():
    from kernel.gates.g5_allowlist import normalise_payee

    assert normalise_payee(" AcmePantry@HDFCBank ")[0] == normalise_payee(PAYEE)[0]
    assert normalise_payee("acmepantry\u200b@hdfcbank")[0] == normalise_payee(PAYEE)[0]
    assert normalise_payee("аcmepantry@hdfcbank")[0] != normalise_payee(PAYEE)[0]  # Cyrillic
    assert normalise_payee("acmepantry@hdfcbank.attacker.in")[0] != normalise_payee(PAYEE)[0]


def test_homoglyph_payee_is_flagged_suspicious():
    from kernel.gates.g5_allowlist import normalise_payee

    _, suspicious = normalise_payee("аcmepantry@hdfcbank")
    assert suspicious


def test_denylist_beats_allowlist(world):
    i = make_intent(world, payees=(PAYEE, "x@upi"), denied_payees=("x@upi",))
    c = make_cart(world, i, payee="x@upi")
    v = world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    assert v.reason == Reason.ALLOW_DENYLIST_HIT


def test_empty_allowlist_cannot_even_be_constructed():
    """Fail-closed, one layer earlier than gate 5: a mandate that scopes neither
    SKUs nor categories is not a permissive mandate, it is an invalid one. Catching
    it in the model means it can never be signed, let alone evaluated."""
    from pydantic import ValidationError

    w = build_world()
    with pytest.raises(ValidationError, match="either SKUs or categories"):
        make_intent(w, categories=(), skus=())


# ──────────────────────────────────────────────────── gate 6: price

def test_merchant_signature_does_not_make_bad_math_true(world):
    i = make_intent(world)
    c = make_cart(world, i, force_total=1)
    v = world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    assert v.reason == Reason.PRICE_CART_TOTAL


def test_cartless_refund_is_bounded_by_settled_spend(world):
    """Regression: the first version of gate 6 returned ok() for any cartless
    action, so a refund of any size passed all eight gates."""
    i = make_intent(world)
    c = make_cart(world, i)
    world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    world.store.commit_reservation(i.mandate_id, c.total_paise)

    over = make_action(world, i, None, action=ActionKind.CREATE_REFUND,
                       attempt_class=AttemptClass.COMPENSATION,
                       amount=c.total_paise * 5, reference_id="pay_1")
    v = world.kernel.evaluate(make_request(world, i, None, over))
    assert v.reason == Reason.PRICE_REFUND_EXCEEDS_SETTLED


def test_refund_without_any_settled_payment_is_denied(world):
    i = make_intent(world)
    a = make_action(world, i, None, action=ActionKind.CREATE_REFUND,
                    attempt_class=AttemptClass.COMPENSATION, amount=1_000,
                    reference_id="pay_1")
    v = world.kernel.evaluate(make_request(world, i, None, a))
    assert v.reason == Reason.PRICE_NO_SETTLED_PAYMENT


# ─────────────────────────────────────────────────── gate 7: velocity

def test_breaker_opens_after_consecutive_denials():
    w = build_world(breaker_denial_threshold=3)
    i = make_intent(w, max_per_txn=1_000, max_total=1_000_000, max_txns=50, rate_per_minute=600)
    for n in range(3):
        c = make_cart(w, i, items=((f"SKU-{n}", "x", "groceries", 1, 90_000 + n),), shipping=0,
                      tax_rate_bp=0)
        w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
    c = make_cart(w, i, items=(("SKU-OK", "ok", "groceries", 1, 500),), shipping=0, tax_rate_bp=0)
    v = w.kernel.evaluate(make_request(w, i, c, make_action(w, i, c)))
    assert v.reason == Reason.VEL_BREAKER_OPEN


def test_replay_denial_does_not_trip_the_breaker():
    """A replayed request is a client bug, not an attack signal. Counting it would
    let a flaky retry loop lock a legitimate user out of their own mandate."""
    w = build_world(breaker_denial_threshold=2)
    _, _, _, req = happy_path(w, max_txns=5, rate_per_minute=600)
    w.kernel.evaluate(req)
    for _ in range(4):
        w.kernel.evaluate(req)
    assert w.store.spend_state(req.intent.payload["mandate_id"])["denial_streak"] == 0


def test_kill_switch_stops_everything(world):
    world.store.flag_set("kill_switch", "1")
    _, _, _, req = happy_path(world)
    v = world.kernel.evaluate(req)
    assert v.reason == Reason.VEL_KILL_SWITCH


def test_success_resets_the_denial_streak(world):
    i = make_intent(world, max_per_txn=200_000, max_total=1_000_000, max_txns=10,
                    rate_per_minute=600)
    bad = make_cart(world, i, items=(("SKU-BIG", "big", "groceries", 1, 300_000),), shipping=0,
                    tax_rate_bp=0)
    world.kernel.evaluate(make_request(world, i, bad, make_action(world, i, bad)))
    assert world.store.spend_state(i.mandate_id)["denial_streak"] == 1

    good = make_cart(world, i, items=(("SKU-OK", "ok", "groceries", 1, 5_000),), shipping=0,
                     tax_rate_bp=0)
    world.kernel.evaluate(make_request(world, i, good, make_action(world, i, good)))
    assert world.store.spend_state(i.mandate_id)["denial_streak"] == 0


# ─────────────────────────────────────────────── gate 8: idempotency

def test_same_cart_twice_is_caught_even_with_fresh_nonces(world):
    i = make_intent(world, max_txns=5, rate_per_minute=600)
    c = make_cart(world, i)
    assert world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c))).allowed
    v = world.kernel.evaluate(make_request(world, i, c, make_action(world, i, c)))
    assert v.reason.startswith("G8_IDEM")


def test_retry_reuses_the_key_escalation_does_not(world):
    from kernel.gates.g8_idempotency import derive_key

    i = make_intent(world)
    c = make_cart(world, i)
    def key(action):
        return derive_key(mandate_id=i.mandate_id, action=action.action,
                          cart_hash=digest(c.signable()), reference_id=action.reference_id,
                          amount_paise=action.amount_paise,
                          attempt_class=action.attempt_class, attempt=action.attempt)

    base = make_action(world, i, c)
    retry = make_action(world, i, c, attempt=2, attempt_class=AttemptClass.RETRY,
                        action_id=base.action_id)
    esc = make_action(world, i, c, attempt=2, attempt_class=AttemptClass.ESCALATION,
                      action_id=base.action_id)
    assert key(base) == key(retry), "a retry must hit the same key or it double-charges"
    assert key(base) != key(esc), "an escalation is a new payment attempt, not a retry"


def test_idempotency_key_is_not_client_supplied(world):
    """The client can change client_nonce all it likes; the key is derived from
    what the money actually is."""
    from kernel.gates.g8_idempotency import derive_key

    i = make_intent(world)
    c = make_cart(world, i)
    a1 = make_action(world, i, c, client_nonce="aaa")
    a2 = make_action(world, i, c, client_nonce="bbb", action_id=a1.action_id)
    common = dict(mandate_id=i.mandate_id, action=a1.action, cart_hash=digest(c.signable()),
                  reference_id=None, amount_paise=a1.amount_paise,
                  attempt_class=a1.attempt_class, attempt=1)
    assert derive_key(**common) == derive_key(**common)
    assert a1.client_nonce != a2.client_nonce  # differing client input, identical key
