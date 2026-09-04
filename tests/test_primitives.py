"""Money, canonicalisation and crypto. The boring layer that everything else
depends on, so it gets tested first and hardest."""
from __future__ import annotations

import pytest

from kernel.canonical import CanonicalisationError, canonical_bytes, digest
from kernel.crypto import (
    KeyPair,
    KeyRegistry,
    KeyRole,
    VerifyResult,
    sign_payload,
    verify_envelope,
)
from kernel.money import MoneyError, add, from_rupee_string, mul, paise, to_rupee_string


# ───────────────────────────────────────────────────────────────── money

def test_rupee_string_never_uses_float():
    assert from_rupee_string("1234.56") == 123_456
    assert from_rupee_string("₹1,234.56") == 123_456
    assert from_rupee_string("0.01") == 1
    assert from_rupee_string("4000.00") == 400_000  # the classic 100x bug


@pytest.mark.parametrize("bad", ["1.234", "abc", "", "1,2.3.4", "-5.00", "1e5"])
def test_rupee_string_rejects_garbage(bad):
    with pytest.raises(MoneyError):
        from_rupee_string(bad)


def test_display_round_trips():
    for amount in (1, 99, 100, 123_456, 10**12):
        assert from_rupee_string(to_rupee_string(amount)) == amount


def test_paise_rejects_float_and_bool():
    with pytest.raises(MoneyError):
        paise(10.5)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        paise(True)  # type: ignore[arg-type]


def test_arithmetic_guards_overflow():
    with pytest.raises(MoneyError):
        add(10**13, 1)
    with pytest.raises(MoneyError):
        mul(10**12, 100)


def test_mul_rejects_zero_and_negative_quantity():
    with pytest.raises(MoneyError):
        mul(100, 0)
    with pytest.raises(MoneyError):
        mul(100, -1)


# ──────────────────────────────────────────────────────── canonicalisation

def test_key_order_does_not_change_digest():
    a = {"b": 1, "a": 2, "c": [1, 2, {"z": 1, "y": 2}]}
    b = {"c": [1, 2, {"y": 2, "z": 1}], "a": 2, "b": 1}
    assert digest(a) == digest(b)


def test_unicode_is_stable():
    d1 = digest({"name": "बासमती चावल"})
    d2 = digest({"name": "बासमती चावल"})
    assert d1 == d2 and len(d1) == 64


def test_floats_are_refused():
    with pytest.raises(CanonicalisationError):
        canonical_bytes({"amount": 10.5})


def test_non_string_keys_refused():
    with pytest.raises(CanonicalisationError):
        canonical_bytes({1: "a"})


def test_cycles_refused():
    d: dict = {}
    d["self"] = d
    with pytest.raises(CanonicalisationError):
        canonical_bytes(d)


def test_deep_nesting_refused():
    node: dict = {"v": 1}
    for _ in range(100):
        node = {"n": node}
    with pytest.raises(CanonicalisationError):
        canonical_bytes(node)


def test_whitespace_is_not_significant_but_content_is():
    assert digest({"a": "x"}) != digest({"a": "x "})


# ───────────────────────────────────────────────────────────────── crypto

def test_signature_round_trip():
    reg = KeyRegistry()
    kp = KeyPair.generate(KeyRole.AGENT, "agent_1")
    reg.register(kp)
    # verify_envelope works on the wire dict, not the pydantic model: signatures
    # must be checkable before any schema opinion is applied.
    env = sign_payload(kp, {"hello": "world"})
    res, rec = verify_envelope(reg, env, KeyRole.AGENT)
    assert res is VerifyResult.OK and rec is not None


def test_tampered_payload_fails():
    reg = KeyRegistry()
    kp = KeyPair.generate(KeyRole.AGENT, "agent_1")
    reg.register(kp)
    env = sign_payload(kp, {"amount": 100})
    env["payload"]["amount"] = 100_000
    res, _ = verify_envelope(reg, env, KeyRole.AGENT)
    assert res is VerifyResult.INVALID


def test_role_confusion_is_rejected():
    reg = KeyRegistry()
    agent = KeyPair.generate(KeyRole.AGENT, "agent_1")
    reg.register(agent)
    env = sign_payload(agent, {"x": 1})
    res, _ = verify_envelope(reg, env, KeyRole.USER)
    assert res is not VerifyResult.OK


def test_revoked_key_is_rejected():
    reg = KeyRegistry()
    kp = KeyPair.generate(KeyRole.AGENT, "agent_1")
    reg.register(kp)
    reg.revoke(kp.key_id)
    res, _ = verify_envelope(reg, sign_payload(kp, {"x": 1}), KeyRole.AGENT)
    assert res is VerifyResult.REVOKED


def test_unknown_key_is_rejected():
    reg = KeyRegistry()
    stranger = KeyPair.generate(KeyRole.AGENT, "nobody")
    res, _ = verify_envelope(reg, sign_payload(stranger, {"x": 1}), KeyRole.AGENT)
    assert res is VerifyResult.UNKNOWN_KEY


def test_alg_downgrade_is_rejected():
    reg = KeyRegistry()
    kp = KeyPair.generate(KeyRole.AGENT, "agent_1")
    reg.register(kp)
    env = sign_payload(kp, {"x": 1})
    env["sig"]["alg"] = "none"
    res, _ = verify_envelope(reg, env, KeyRole.AGENT)
    assert res is VerifyResult.BAD_ALG


def test_deterministic_keys_from_seed():
    a = KeyPair.from_seed(KeyRole.USER, "u", b"0" * 32)
    b = KeyPair.from_seed(KeyRole.USER, "u", b"0" * 32)
    assert a.key_id == b.key_id


def test_key_id_is_bound_to_public_key():
    a = KeyPair.generate(KeyRole.USER, "u")
    b = KeyPair.generate(KeyRole.USER, "u")
    assert a.key_id != b.key_id  # same subject, different key -> different id
