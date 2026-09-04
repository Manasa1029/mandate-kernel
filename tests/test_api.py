"""HTTP surface tests via FastAPI's TestClient — no server, no network.

The API module builds its store from the environment at import time, so the
env is set before the import below. That is a deliberate constraint of a
single-process demo service; production would use a dependency-injected store.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_DB = os.path.join(tempfile.mkdtemp(prefix="mk-api-"), "api-test.db")
os.environ["KERNEL_DB_PATH"] = _DB
os.environ["RAZORPAY_MODE"] = "mock"
os.environ["KERNEL_MAX_ATTEMPTS"] = "3"

from kernel.api import app  # noqa: E402  (import after env setup, on purpose)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _intent(client, **over):
    body = {
        "prompt_playback": "Buy up to ₹5,000 of groceries from Acme Pantry.",
        "constraints": {
            "max_total_paise": 500_000, "max_per_txn_paise": 200_000,
            "max_transactions": 5, "rate_per_minute": 60,
            "allowed_merchants": ["acme_pantry"], "allowed_payees": ["acmepantry@hdfcbank"],
            "allowed_categories": ["groceries"], "allowed_skus": [],
        },
        "ttl_seconds": 3600, "human_present": True,
    }
    body.update(over)
    r = client.post("/v1/mandates/intent", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _mandate_id(client, **over) -> str:
    return _intent(client, **over)["mandate_id"]


# ───────────────────────────────────────────────────── read routes

def test_healthz_reports_an_intact_ledger(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["ledger_intact"] is True
    assert body["provider"].startswith("mock")


def test_keys_route_never_leaks_private_material(client):
    body = client.get("/v1/keys").json()
    blob = repr(body).lower()
    assert "private" not in blob and "secret" not in blob and "seed" not in blob
    assert body["alg"] == "Ed25519"
    assert body["user"] and body["agent_delegated"] and body["merchant"]
    # The rogue key is published as *registered but not delegated* — a verifiable
    # signature is not the same thing as authority to spend.
    assert body["agent_rogue_registered_but_not_delegated"] != body["agent_delegated"]


def test_ledger_verify_route(client):
    body = client.get("/v1/ledger/verify").json()
    assert body["intact"] is True
    assert body["first_bad_seq"] is None


def test_ledger_limit_is_respected(client):
    _intent(client)
    _intent(client)
    body = client.get("/v1/ledger", params={"limit": 1}).json()
    assert len(body["entries"]) == 1


def test_ledger_limit_is_clamped_at_both_ends(client):
    """SQLite reads a negative LIMIT as unlimited, so an unclamped lower bound turns
    `?limit=-1` into a full ledger dump from a single unauthenticated GET."""
    for _ in range(4):
        _intent(client)
    for hostile in (-1, 0, -10_000):
        entries = client.get("/v1/ledger", params={"limit": hostile}).json()["entries"]
        assert len(entries) == 1, f"limit={hostile} escaped the clamp"
    capped = client.get("/v1/ledger", params={"limit": 10_000}).json()["entries"]
    assert len(capped) <= 500


# ────────────────────────────────────────────── mandate issuance

def test_intent_route_returns_a_signed_envelope(client):
    body = _intent(client)
    assert body["mandate_id"].startswith("mnd_")
    env = body["intent"]
    assert env["sig"]["value"] and env["sig"]["alg"] == "Ed25519"
    assert env["sig"]["key_id"].startswith("ed25519:")
    assert env["payload"]["prompt_playback"].startswith("Buy up to")


def test_intent_route_rejects_unknown_fields(client):
    r = client.post("/v1/mandates/intent", json={
        "prompt_playback": "x",
        "constraints": {"max_total_paise": 1000, "max_per_txn_paise": 1000,
                        "max_transactions": 1, "allowed_categories": ["groceries"]},
        "surprise_field": True})
    assert r.status_code == 422, "extra=forbid must reject unknown input"


def test_intent_route_rejects_negative_limits(client):
    r = client.post("/v1/mandates/intent", json={
        "prompt_playback": "x",
        "constraints": {"max_total_paise": -1, "max_per_txn_paise": 1000,
                        "max_transactions": 1, "allowed_categories": ["groceries"]}})
    assert r.status_code == 422


def test_intent_route_rejects_a_per_txn_cap_above_the_total(client):
    r = client.post("/v1/mandates/intent", json={
        "prompt_playback": "x",
        "constraints": {"max_total_paise": 1000, "max_per_txn_paise": 100_000,
                        "max_transactions": 1, "allowed_categories": ["groceries"]}})
    assert r.status_code == 422


# ───────────────────────────────────────────────── state routes

def test_mandate_state_route(client):
    mid = _mandate_id(client)
    state = client.get(f"/v1/mandates/{mid}/state").json()
    assert state["committed_paise"] == 0
    assert state["reserved_paise"] == 0
    assert state["revoked"] is False
    assert state["headroom_paise"] == 500_000


def test_unknown_mandate_state_is_404(client):
    assert client.get("/v1/mandates/mnd_nope/state").status_code == 404


def test_revoke_is_reflected_in_state(client):
    mid = _mandate_id(client)
    r = client.post(f"/v1/mandates/{mid}/revoke", json={"reason": "user changed their mind"})
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert client.get(f"/v1/mandates/{mid}/state").json()["revoked"] is True


def test_revoking_twice_is_idempotent(client):
    mid = _mandate_id(client)
    client.post(f"/v1/mandates/{mid}/revoke", json={"reason": "first"})
    r = client.post(f"/v1/mandates/{mid}/revoke", json={"reason": "second"})
    assert r.status_code == 200 and r.json()["revoked"] is True


def test_trace_of_an_unknown_action_is_404(client):
    r = client.get("/v1/trace/act_does_not_exist")
    assert r.status_code == 404


# ─────────────────────────────────────────── malformed proposals

def test_evaluate_rejects_a_body_that_is_not_a_kernel_request(client):
    r = client.post("/v1/evaluate", json={"hello": "world"})
    assert r.status_code == 422


def test_evaluate_rejects_a_float_amount(client):
    """StrictInt on the wire is the first line of defence against 1308.9999."""
    r = client.post("/v1/evaluate", json={"action": {"amount_paise": 1308.99}})
    assert r.status_code == 422


def test_execute_rejects_an_unminted_capability(client):
    r = client.post("/v1/execute", json={"capability_token": "cap_" + "0" * 40})
    assert r.status_code in (400, 403, 404, 409)
    assert "cap" in r.text.lower() or "unknown" in r.text.lower()


def test_execute_rejects_an_unknown_field(client):
    r = client.post("/v1/execute", json={"capability_token": "x", "amount_paise": 999})
    assert r.status_code == 422


def test_compensate_requires_every_field(client):
    r = client.post("/v1/compensate", json={"mandate_id": "mnd_1"})
    assert r.status_code == 422


# ───────────────────────────────────────────────── kill switch

def test_kill_switch_toggles_and_is_visible_in_health(client):
    assert client.post("/v1/admin/kill-switch", json={"on": True}).status_code == 200
    assert client.get("/healthz").json()["kill_switch"] is True
    assert client.post("/v1/admin/kill-switch", json={"on": False}).status_code == 200
    assert client.get("/healthz").json()["kill_switch"] is False


def test_kill_switch_change_is_written_to_the_ledger(client):
    client.post("/v1/admin/kill-switch", json={"on": True})
    client.post("/v1/admin/kill-switch", json={"on": False})
    kinds = [e["kind"] for e in client.get("/v1/ledger", params={"limit": 20}).json()["entries"]]
    assert any("kill" in k for k in kinds)


def test_the_ledger_is_still_intact_after_every_test(client):
    assert client.get("/v1/ledger/verify").json()["intact"] is True


# ───────────────────────────────────────────────────── webhooks

def _signed(body: bytes, secret: str = "test-webhook-secret") -> str:
    import hashlib
    import hmac
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test-webhook-secret")


def test_verified_webhook_is_accepted_and_logged(client):
    import json as _json
    body = _json.dumps({"event": "payment.captured",
                        "payload": {"payment": {"entity": {"id": "pay_ABC"}}}}).encode()
    r = client.post("/v1/webhooks/razorpay", content=body,
                    headers={"x-razorpay-signature": _signed(body),
                             "content-type": "application/json"})
    assert r.status_code == 200 and r.json()["event"] == "payment.captured"
    top = client.get("/v1/ledger", params={"limit": 1}).json()["entries"][0]
    assert top["kind"] == "webhook.accepted"
    assert top["payload"]["entity_ids"] == ["pay_ABC"]


def test_forged_webhook_is_rejected_but_still_recorded(client):
    """Dropping forged callbacks silently throws away the evidence that someone
    is probing the endpoint."""
    r = client.post("/v1/webhooks/razorpay", content=b'{"event":"payment.captured"}',
                    headers={"x-razorpay-signature": "deadbeef" * 8})
    assert r.status_code == 400
    top = client.get("/v1/ledger", params={"limit": 1}).json()["entries"][0]
    assert top["kind"] == "webhook.rejected"
    assert top["payload"]["verified"] is False


def test_unsigned_webhook_is_rejected(client):
    r = client.post("/v1/webhooks/razorpay", content=b'{"event":"payment.captured"}')
    assert r.status_code == 400


def test_webhook_with_a_valid_signature_over_different_bytes_is_rejected(client):
    """The classic mistake: verifying a re-serialised body. The signature must be
    checked against the exact bytes received."""
    sig = _signed(b'{"event":"payment.captured"}')
    r = client.post("/v1/webhooks/razorpay", content=b'{"event": "payment.captured"}',
                    headers={"x-razorpay-signature": sig})
    assert r.status_code == 400


def test_unparseable_webhook_body_does_not_500(client):
    body = b"this is not json"
    r = client.post("/v1/webhooks/razorpay", content=body,
                    headers={"x-razorpay-signature": _signed(body)})
    assert r.status_code == 200
    top = client.get("/v1/ledger", params={"limit": 1}).json()["entries"][0]
    assert top["payload"]["event"] == "unparseable"


def test_a_webhook_never_moves_money_on_its_own(client):
    """A verified webhook claiming a huge capture must not change any budget."""
    import json as _json
    mid = _mandate_id(client)
    before = client.get(f"/v1/mandates/{mid}/state").json()
    body = _json.dumps({"event": "payment.captured", "mandate_id": mid,
                        "payload": {"payment": {"entity": {"id": "pay_X",
                                                           "amount": 9_999_999}}}}).encode()
    client.post("/v1/webhooks/razorpay", content=body,
                headers={"x-razorpay-signature": _signed(body)})
    assert client.get(f"/v1/mandates/{mid}/state").json() == before


def test_compensate_refuses_an_unbounded_refund_with_502(client):
    """The route bypasses the gate pipeline, so it must still refuse a refund that
    no recorded spend backs — and answer 502, not a 500 traceback."""
    r = client.post("/v1/compensate", json={"mandate_id": "mnd_never_used",
                                            "payment_id": "pay_invented",
                                            "amount_paise": 9_999_00,
                                            "cause": "refund drain"})
    assert r.status_code == 502
    assert r.json()["execution"]["reason"].startswith("G6_PRICE")
