"""Execution failure paths. This file is the one that would have caught the
double-charge bugs, so every branch of the state machine is exercised:
success, transient retry, hard rejection, unknown state, stop rule, compensation.
"""
from __future__ import annotations

import pytest

from adapters.base import ProviderRejected, ProviderRetriable, ProviderUnknownState
from adapters.mock_razorpay import Fail, MockRazorpay
from kernel.errors import Reason
from kernel.executor import Executor
from tests.factories import build_world, happy_path


def _allowed(world, **intent_kw):
    intent, cart, action, req = happy_path(world, **intent_kw)
    verdict = world.kernel.evaluate(req)
    assert verdict.allowed, verdict.reason
    return intent, cart, action, verdict


def test_happy_execution_settles_and_commits(world, provider, sleeper):
    intent, cart, action, verdict = _allowed(world)
    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)

    assert out.state == "done" and out.succeeded
    assert out.provider_id
    assert out.attempts == 1
    state = world.store.spend_state(intent.mandate_id)
    assert state["committed"] == cart.total_paise
    assert state["reserved"] == 0
    assert sleeper.calls == []


def test_transient_failure_retries_with_the_same_key(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderRetriable, "SERVER_ERROR")])
    _, _, action, verdict = _allowed(world)

    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert out.state == "done"
    assert out.attempts == 2
    keys = {c.get("idempotency_key") for c in provider.calls if c["op"] == "create_order"}
    assert len(keys) == 1, "a retry that changes the idempotency key is a double charge"
    assert sleeper.calls == [pytest.approx(0.2)]


def test_backoff_is_exponential_and_capped(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderRetriable, "SERVER_ERROR")] * 5)
    _, _, action, verdict = _allowed(world)

    Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert sleeper.calls == [pytest.approx(0.2), pytest.approx(0.4)]  # 3rd attempt is the last


def test_retries_are_capped_and_open_the_breaker(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderRetriable, "SERVER_ERROR")] * 10)
    intent, cart, action, verdict = _allowed(world)

    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert out.state == "stopped"
    assert out.attempts == world.cfg.max_attempts_per_cart
    assert out.requires_human
    state = world.store.spend_state(intent.mandate_id)
    assert state["reserved"] == 0, "a stopped attempt must give the headroom back"
    assert state["denial_streak"] >= 1


def test_hard_rejection_is_not_retried(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderRejected, "BAD_REQUEST_ERROR")])
    intent, cart, action, verdict = _allowed(world)

    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert out.state == "failed"
    assert out.attempts == 1, "a 400 does not become a 200 by asking again"
    assert world.store.spend_state(intent.mandate_id)["reserved"] == 0


def test_declines_advise_escalation_but_do_not_self_escalate(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderRejected, "GATEWAY_ERROR")])
    _, _, action, verdict = _allowed(world)

    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert out.state == "failed"
    assert out.escalation_advised
    # Escalation is advice to the agent, not an action the executor takes: switching
    # instruments needs a fresh trip through the kernel with a new idempotency key.
    assert out.provider_id is None


def test_unknown_state_is_reconciled_when_the_write_landed(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderUnknownState, "TIMEOUT", landed=True)])
    intent, cart, action, verdict = _allowed(world)

    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert out.state == "done", "the write landed; reconciliation must find it"
    assert world.store.spend_state(intent.mandate_id)["committed"] == cart.total_paise


def test_unknown_state_freezes_when_reconciliation_fails(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderUnknownState, "TIMEOUT", landed=False)])
    intent, cart, action, verdict = _allowed(world)

    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert out.state == "unknown"
    assert out.requires_human
    # The critical assertion: money that might have moved stays reserved.
    assert world.store.spend_state(intent.mandate_id)["reserved"] == cart.total_paise
    assert world.store.flag_get("kill_switch", "0") == "1"


def test_stop_rule_blocks_a_fourth_attempt_before_touching_the_provider(world, sleeper):
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderRetriable, "SERVER_ERROR")] * 10)
    _, _, action, verdict = _allowed(world)
    ex = Executor(world.store, provider, world.cfg, sleeper)
    ex.execute(verdict, action)
    calls_before = len(provider.calls)

    world.store.flag_set("kill_switch", "0")
    _, _, action2, verdict2 = _allowed(world, max_txns=5, rate_per_minute=600)
    # Force the same attempts counter as the exhausted cart.
    world.store.flag_set(f"attempts:{verdict2.capability.idempotency_key}",
                         str(world.cfg.max_attempts_per_cart))
    out = ex.execute(verdict2, action2)
    assert out.state == "stopped"
    assert len(provider.calls) == calls_before, "stop rule must fire before any network call"


def test_capability_is_single_use(world, provider, sleeper):
    _, _, action, verdict = _allowed(world)
    ex = Executor(world.store, provider, world.cfg, sleeper)
    assert ex.execute(verdict, action).state == "done"
    second = ex.execute(verdict, action)
    assert second.state == "failed"
    assert second.reason.startswith("X_CAP") or "cap" in second.reason.lower()


def test_capability_amount_is_re_verified_at_redemption(world, provider, sleeper):
    """Even with a valid token, the executor re-checks amount and payee. A token
    that is valid for ₹1,308 cannot be spent on ₹13,080."""
    _, _, action, verdict = _allowed(world)
    inflated = action.model_copy(update={"amount_paise": action.amount_paise * 10})
    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, inflated)
    assert out.state == "failed"


def test_compensation_refunds_and_is_logged(world, provider, sleeper):
    intent, cart, action, verdict = _allowed(world)
    ex = Executor(world.store, provider, world.cfg, sleeper)
    out = ex.execute(verdict, action)
    payment = provider.simulate_customer_payment(out.provider_id, authorize_only=False)

    comp = ex.compensate(mandate_id=intent.mandate_id, payment_id=payment.provider_id,
                         amount_paise=cart.total_paise, cause="seller could not fulfil",
                         action_id=action.action_id)
    assert comp.state == "compensated"
    assert world.store.spend_state(intent.mandate_id)["committed"] == 0
    events = [e["kind"] for e in world.store.trace(action.action_id)]
    assert any("compensat" in k for k in events)


def test_compensation_works_even_with_the_kill_switch_engaged(world, provider, sleeper):
    """Deliberate asymmetry: the kill switch stops money going out, never money
    coming back. A frozen system that cannot refund traps customer funds."""
    intent, cart, action, verdict = _allowed(world)
    ex = Executor(world.store, provider, world.cfg, sleeper)
    out = ex.execute(verdict, action)
    payment = provider.simulate_customer_payment(out.provider_id, authorize_only=False)

    world.store.flag_set("kill_switch", "1")
    comp = ex.compensate(mandate_id=intent.mandate_id, payment_id=payment.provider_id,
                         amount_paise=cart.total_paise, cause="frozen but must refund",
                         action_id=action.action_id)
    assert comp.state == "compensated"


def test_ledger_chain_survives_every_failure_path(world, sleeper):
    provider = MockRazorpay()
    provider.script([
        Fail("create_order", ProviderRetriable, "SERVER_ERROR"),
        Fail("create_order", ProviderRejected, "BAD_REQUEST_ERROR"),
    ])
    for _ in range(3):
        try:
            _, _, action, verdict = _allowed(world, max_txns=9, rate_per_minute=600)
        except AssertionError:
            break
        Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    ok, bad_seq, detail = world.store.verify_chain()
    assert ok, f"chain broken at {bad_seq}: {detail}"


def test_reconciliation_does_not_sleep_after_its_final_probe(world, sleeper):
    """Three probes need two gaps. A third sleep delays the freeze decision and the
    kill switch by 0.6s for no possible benefit — nothing follows it."""
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderUnknownState, "TIMEOUT", landed=False)])
    _, _, action, verdict = _allowed(world)

    out = Executor(world.store, provider, world.cfg, sleeper).execute(verdict, action)
    assert out.state == "unknown"
    assert sleeper.calls == [0.2, 0.4], sleeper.calls


def test_compensation_survives_a_transient_provider_failure(world, sleeper):
    """A refund that fails transiently must not escape as an unhandled exception:
    that surfaces as an opaque HTTP 500 with no ledger entry and no human flag."""
    provider = MockRazorpay()
    intent, cart, action, verdict = _allowed(world)
    ex = Executor(world.store, provider, world.cfg, sleeper)
    out = ex.execute(verdict, action)
    payment = provider.simulate_customer_payment(out.provider_id, authorize_only=False)
    provider.script([Fail("create_refund", ProviderRetriable, "SERVER_ERROR")])

    comp = ex.compensate(mandate_id=intent.mandate_id, payment_id=payment.provider_id,
                         amount_paise=cart.total_paise, cause="fulfilment failed",
                         action_id=action.action_id)
    assert comp.state == "failed"
    assert comp.requires_human
    # The refund is unproven, so the spend must not be credited back.
    assert world.store.spend_state(intent.mandate_id)["committed"] == cart.total_paise
    assert "exec.compensation_unresolved" in [e["kind"] for e in world.store.trace(action.action_id)]


def test_compensation_with_unknown_provider_state_is_never_blind_retried(world, sleeper):
    provider = MockRazorpay()
    intent, cart, action, verdict = _allowed(world)
    ex = Executor(world.store, provider, world.cfg, sleeper)
    out = ex.execute(verdict, action)
    payment = provider.simulate_customer_payment(out.provider_id, authorize_only=False)
    provider.script([Fail("create_refund", ProviderUnknownState, "TIMEOUT", landed=True)])

    comp = ex.compensate(mandate_id=intent.mandate_id, payment_id=payment.provider_id,
                         amount_paise=cart.total_paise, cause="fulfilment failed",
                         action_id=action.action_id)
    assert comp.state == "unknown"
    assert comp.requires_human
    refunds = [c for c in provider.calls if c["op"] == "create_refund"]
    assert len(refunds) == 1, "a refund that may have landed must never be retried"


def test_compensation_amount_is_bounded_by_the_mandate_spend(world, provider, sleeper):
    """The direct compensation path skips the gates, so it carries its own bound.
    Without it, /v1/compensate is an unbounded refund primitive."""
    intent, cart, action, verdict = _allowed(world)
    ex = Executor(world.store, provider, world.cfg, sleeper)
    out = ex.execute(verdict, action)
    payment = provider.simulate_customer_payment(out.provider_id, authorize_only=False)

    comp = ex.compensate(mandate_id=intent.mandate_id, payment_id=payment.provider_id,
                         amount_paise=cart.total_paise * 50, cause="over-refund attempt",
                         action_id=action.action_id)
    assert comp.state == "failed"
    assert comp.reason == str(Reason.PRICE_REFUND_EXCEEDS_SETTLED)
    assert not [c for c in provider.calls if c["op"] == "create_refund"]


def test_compensation_is_refused_for_a_mandate_that_never_spent(world, provider, sleeper):
    comp = Executor(world.store, provider, world.cfg, sleeper).compensate(
        mandate_id="mnd_never_used", payment_id="pay_invented", amount_paise=500_00,
        cause="refund drain against an unknown mandate")
    assert comp.state == "failed"
    assert comp.reason == str(Reason.PRICE_NO_SETTLED_PAYMENT)
    assert not [c for c in provider.calls if c["op"] == "create_refund"]
