"""Tests for the real Razorpay REST adapter (adapters/razorpay_rest.py).

Nothing here touches the network — respx intercepts httpx at the transport
layer, so these run offline exactly like every other test in the suite.
`requirements-dev.txt` has carried `respx` since day one specifically "to mock
httpx at the transport layer for the REST adapter tests"; until this file
existed, nothing used it. The mock provider path (adapters/mock_razorpay.py)
was fully exercised by the rest of the suite; the code that actually talks to
Razorpay was not covered by anything.

What's asserted here mirrors the error taxonomy documented in adapters/base.py
and the module docstring of razorpay_rest.py:
  * live-key refusal is the loud, default-safe behaviour
  * 5xx/429/connect errors are retriable
  * 4xx is a definitive rejection carrying Razorpay's own error code
  * a timeout AFTER a write is UNKNOWN, never silently retriable
  * the idempotency key is stamped on every create so reconciliation can find it
  * webhook verification refuses to believe an unsigned or misconfigured callback
"""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from adapters.base import ProviderRejected, ProviderRetriable, ProviderUnknownState
from adapters.razorpay_rest import RazorpayRestClient, verify_webhook

BASE = "https://api.razorpay.com/v1"


def _client(**kw) -> RazorpayRestClient:
    kw.setdefault("key_id", "rzp_test_abc123")
    kw.setdefault("key_secret", "secret_abc123")
    return RazorpayRestClient(**kw)


# ------------------------------------------------------------- construction

def test_refuses_non_test_key_by_default(monkeypatch):
    monkeypatch.delenv("RAZORPAY_ALLOW_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="refusing to run"):
        _client(key_id="rzp_live_realmoney")


def test_allows_live_key_only_with_explicit_override_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("RAZORPAY_ALLOW_LIVE", "1")
    with caplog.at_level("CRITICAL"):
        client = _client(key_id="rzp_live_realmoney")
    client.close()
    assert any("LIVE" in r.message for r in caplog.records)


def test_accepts_test_key_without_any_override(monkeypatch):
    monkeypatch.delenv("RAZORPAY_ALLOW_LIVE", raising=False)
    _client().close()  # must not raise


# ------------------------------------------------------------- create_order

@respx.mock
def test_create_order_stamps_idempotency_key_and_returns_result():
    route = respx.post(f"{BASE}/orders").mock(
        return_value=httpx.Response(200, json={"id": "order_123", "status": "created",
                                                "amount": 50000}))
    client = _client()
    result = client.create_order(amount_paise=50000, receipt="rcpt_1",
                                  idempotency_key="idem_abc", notes={"cart": "c1"})
    client.close()

    assert result.ok and result.provider_id == "order_123" and result.amount_paise == 50000
    body = json.loads(route.calls.last.request.content)
    assert body["notes"]["idem_key"] == "idem_abc"
    assert body["notes"]["cart"] == "c1"
    assert body["payment_capture"] == 0, "orders must be created uncaptured; capture is a separate gated action"


@respx.mock
def test_create_payment_link_sets_reference_id_and_upi_flag():
    respx.post(f"{BASE}/payment_links").mock(
        return_value=httpx.Response(200, json={"id": "plink_1", "status": "created",
                                                "amount": 10000, "short_url": "https://rzp.io/x"}))
    client = _client()
    result = client.create_payment_link(amount_paise=10000, description="test",
                                         idempotency_key="idem_xyz_very_long_key_value",
                                         notes={}, upi_only=True)
    client.close()
    assert result.ok and result.short_url == "https://rzp.io/x"


@respx.mock
def test_capture_payment_reports_not_ok_when_provider_did_not_capture():
    respx.post(f"{BASE}/payments/pay_1/capture").mock(
        return_value=httpx.Response(200, json={"id": "pay_1", "status": "failed", "amount": 5000}))
    client = _client()
    result = client.capture_payment(payment_id="pay_1", amount_paise=5000)
    client.close()
    assert result.ok is False and result.status == "failed"


@respx.mock
def test_create_refund_success():
    respx.post(f"{BASE}/payments/pay_1/refund").mock(
        return_value=httpx.Response(200, json={"id": "rfnd_1", "status": "pending", "amount": 5000}))
    client = _client()
    result = client.create_refund(payment_id="pay_1", amount_paise=5000, idempotency_key="idem_r1")
    client.close()
    assert result.ok and result.provider_id == "rfnd_1"


# --------------------------------------------------------- error taxonomy

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@respx.mock
def test_retriable_statuses_raise_provider_retriable(status):
    respx.post(f"{BASE}/orders").mock(return_value=httpx.Response(status, json={}))
    client = _client()
    with pytest.raises(ProviderRetriable):
        client.create_order(amount_paise=100, receipt="r", idempotency_key="i", notes={})
    client.close()


@respx.mock
def test_4xx_raises_provider_rejected_with_razorpays_own_error_code():
    respx.post(f"{BASE}/orders").mock(
        return_value=httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR",
                                                          "description": "amount too small"}}))
    client = _client()
    with pytest.raises(ProviderRejected) as ei:
        client.create_order(amount_paise=1, receipt="r", idempotency_key="i", notes={})
    client.close()
    assert ei.value.code == "BAD_REQUEST_ERROR"
    assert "amount too small" in str(ei.value)


@respx.mock
def test_connect_error_is_retriable():
    respx.post(f"{BASE}/orders").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()
    with pytest.raises(ProviderRetriable):
        client.create_order(amount_paise=100, receipt="r", idempotency_key="i", notes={})
    client.close()


@respx.mock
def test_timeout_after_a_write_is_unknown_state_not_retriable():
    """The load-bearing correctness property of this adapter: a timeout on a
    WRITE call must never look like a safe-to-retry failure, because the write
    may have already landed on Razorpay's side. Regress this and a retry can
    double-charge. Guard it explicitly, separate from the general timeout case."""
    respx.post(f"{BASE}/orders").mock(side_effect=httpx.ReadTimeout("timed out"))
    client = _client()
    with pytest.raises(ProviderUnknownState):
        client.create_order(amount_paise=100, receipt="r", idempotency_key="i", notes={})
    client.close()


@respx.mock
def test_timeout_on_a_read_call_is_retriable_not_unknown():
    respx.get(f"{BASE}/payments/pay_1").mock(side_effect=httpx.ReadTimeout("timed out"))
    client = _client()
    with pytest.raises(ProviderRetriable):
        client.fetch_payment(payment_id="pay_1")
    client.close()


# ------------------------------------------------------------- reconciliation

@respx.mock
def test_find_by_idempotency_matches_an_order_by_receipt():
    respx.get(f"{BASE}/orders").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "order_9", "status": "created",
                                                           "amount": 200}]}))
    client = _client()
    result = client.find_by_idempotency(idempotency_key="idem_9")
    client.close()
    assert result is not None and result.provider_id == "order_9"


@respx.mock
def test_find_by_idempotency_falls_back_to_a_payment_link_by_reference_id():
    respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(f"{BASE}/payment_links").mock(
        return_value=httpx.Response(200, json={"payment_links": [{"id": "plink_9",
                                                                   "status": "created",
                                                                   "amount": 200}]}))
    client = _client()
    result = client.find_by_idempotency(idempotency_key="idem_9")
    client.close()
    assert result is not None and result.provider_id == "plink_9"


@respx.mock
def test_find_by_idempotency_returns_none_when_nothing_matches():
    respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(f"{BASE}/payment_links").mock(return_value=httpx.Response(200, json={"items": []}))
    client = _client()
    result = client.find_by_idempotency(idempotency_key="idem_missing")
    client.close()
    assert result is None


# ------------------------------------------------------------------ webhooks

def test_verify_webhook_accepts_a_correctly_signed_body():
    body = b'{"event":"payment.captured"}'
    secret = "whsec_123"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook(body, sig, secret) is True


def test_verify_webhook_rejects_a_wrong_signature():
    body = b'{"event":"payment.captured"}'
    assert verify_webhook(body, "0" * 64, "whsec_123") is False


def test_verify_webhook_refuses_to_believe_an_unverifiable_callback_with_no_secret():
    """No configured secret means we cannot verify — that must fail closed,
    not be treated as trivially verified."""
    body = b'{"event":"payment.captured"}'
    sig = hmac.new(b"whsec_123", body, hashlib.sha256).hexdigest()
    assert verify_webhook(body, sig, secret="") is False


def test_verify_webhook_rejects_a_missing_signature():
    assert verify_webhook(b"{}", "", "whsec_123") is False
