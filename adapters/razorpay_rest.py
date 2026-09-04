"""Real Razorpay test-mode client over httpx.

Keys live here and nowhere else. Nothing in `kernel/` imports this module; the
API wires it in at startup based on RAZORPAY_MODE.

Mapping to the platform surface (same endpoints the official MCP server wraps):
    POST /v1/orders                       create_order
    POST /v1/payment_links                create_payment_link
    POST /v1/payments/{id}/capture        capture_payment
    POST /v1/payments/{id}/refund         create_refund
    GET  /v1/payments/{id}                fetch_payment
    GET  /v1/orders?receipt=...           reconciliation by receipt

Edge cases handled here:
  * 5xx / 429 / connect errors -> ProviderRetriable.
  * Read timeout AFTER the request was written -> ProviderUnknownState, because
    the order may exist. The executor reconciles instead of retrying.
  * 4xx -> ProviderRejected with Razorpay's own error code preserved.
  * `notes.idem_key` is stamped on every create so reconciliation can find the
    object we may or may not have created.
  * Amounts are sent as integer paise, exactly as received. No float ever.
  * Webhooks are verified with HMAC-SHA256 before they are believed. An
    unverified webhook is an unauthenticated stranger telling you a payment
    succeeded, which is exactly the message an attacker wants to send.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import httpx

from .base import (
    PaymentProvider,
    ProviderRejected,
    ProviderResult,
    ProviderRetriable,
    ProviderUnknownState,
)

BASE = "https://api.razorpay.com/v1"


def log_once_live_warning() -> None:
    import logging
    logging.getLogger("adapters.razorpay").critical(
        "RUNNING AGAINST LIVE RAZORPAY KEYS — every allowed action moves real money")


def verify_webhook(body: bytes, signature: str, secret: str | None = None) -> bool:
    """Razorpay signs webhook bodies with HMAC-SHA256 over the raw bytes.

    Three failure modes deliberately return False rather than raising, so a
    caller cannot accidentally treat "misconfigured" as "verified":
      * no secret configured        -> we cannot verify, so we do not believe it
      * signature absent or malformed
      * digest mismatch

    Compared with `compare_digest` to keep the check constant-time. Verify over
    the RAW body: re-serialising the JSON first changes the bytes and the
    signature will never match.
    """
    secret = secret if secret is not None else os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())
RETRIABLE_STATUS = {429, 500, 502, 503, 504}


class RazorpayRestClient(PaymentProvider):
    name = "razorpay-test"

    def __init__(self, key_id: str | None = None, key_secret: str | None = None,
                 timeout: float = 8.0) -> None:
        self.key_id = key_id or os.environ["RAZORPAY_KEY_ID"]
        self.key_secret = key_secret or os.environ["RAZORPAY_KEY_SECRET"]
        # A live key in a hackathon repo is an incident waiting to happen, so the
        # default is to refuse. The override exists, is explicit, and is loud.
        if not self.key_id.startswith("rzp_test_"):
            if os.environ.get("RAZORPAY_ALLOW_LIVE") != "1":
                raise RuntimeError(
                    "refusing to run against non-test keys; set RAZORPAY_ALLOW_LIVE=1 "
                    "only if you genuinely intend to move real money")
            log_once_live_warning()
        self._client = httpx.Client(base_url=BASE, auth=(self.key_id, self.key_secret),
                                    timeout=httpx.Timeout(timeout, connect=3.0))

    # ------------------------------------------------------------- transport

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None, write: bool = False) -> dict[str, Any]:
        try:
            r = self._client.request(method, path, json=json, params=params)
        except httpx.ConnectError as e:
            raise ProviderRetriable(f"connect failed: {e}", code="CONNECT") from e
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as e:
            if write:
                raise ProviderUnknownState(f"timeout after write: {e}", code="TIMEOUT_AFTER_WRITE") from e
            raise ProviderRetriable(f"timeout: {e}", code="TIMEOUT") from e
        except httpx.HTTPError as e:
            raise ProviderRetriable(f"transport: {e}", code="TRANSPORT") from e

        if r.status_code in RETRIABLE_STATUS:
            raise ProviderRetriable(f"provider {r.status_code}", code=str(r.status_code),
                                    raw=_safe_json(r))
        if r.status_code >= 400:
            body = _safe_json(r)
            err = (body.get("error") or {})
            raise ProviderRejected(err.get("description", r.text[:200]),
                                   code=err.get("code", str(r.status_code)), raw=body)
        return _safe_json(r)

    # ------------------------------------------------------------------- API

    def create_order(self, *, amount_paise: int, receipt: str, idempotency_key: str,
                     notes: dict[str, str]) -> ProviderResult:
        body = {"amount": amount_paise, "currency": "INR", "receipt": receipt,
                "notes": {**notes, "idem_key": idempotency_key}, "payment_capture": 0}
        d = self._request("POST", "/orders", json=body, write=True)
        return ProviderResult(True, d["id"], d.get("status", "created"), d["amount"], "order", d)

    def create_payment_link(self, *, amount_paise: int, description: str, idempotency_key: str,
                            notes: dict[str, str], upi_only: bool = False) -> ProviderResult:
        body: dict[str, Any] = {
            "amount": amount_paise, "currency": "INR", "description": description[:255],
            "notes": {**notes, "idem_key": idempotency_key},
            "reference_id": idempotency_key[:40],
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
        if upi_only:
            body["upi_link"] = True
        d = self._request("POST", "/payment_links", json=body, write=True)
        return ProviderResult(True, d["id"], d.get("status", "created"), d["amount"],
                              "payment_link", d, short_url=d.get("short_url"))

    def capture_payment(self, *, payment_id: str, amount_paise: int) -> ProviderResult:
        d = self._request("POST", f"/payments/{payment_id}/capture",
                          json={"amount": amount_paise, "currency": "INR"}, write=True)
        return ProviderResult(d.get("status") == "captured", d["id"], d.get("status", ""),
                              d["amount"], "payment", d)

    def create_refund(self, *, payment_id: str, amount_paise: int, idempotency_key: str) -> ProviderResult:
        d = self._request("POST", f"/payments/{payment_id}/refund",
                          json={"amount": amount_paise, "notes": {"idem_key": idempotency_key},
                                "speed": "normal"}, write=True)
        return ProviderResult(True, d["id"], d.get("status", "pending"), d["amount"], "refund", d)

    def fetch_payment(self, *, payment_id: str) -> ProviderResult:
        d = self._request("GET", f"/payments/{payment_id}")
        return ProviderResult(d.get("status") not in {"failed", None}, d["id"], d.get("status", ""),
                              d["amount"], "payment", d)

    def find_by_idempotency(self, *, idempotency_key: str) -> ProviderResult | None:
        """Reconcile after an unknown-state write.

        Orders carry our key in `receipt`; payment links in `reference_id`. Both
        are searchable, which is why we set them on create.
        """
        d = self._request("GET", "/orders", params={"receipt": idempotency_key, "count": 1})
        items = d.get("items") or []
        if items:
            o = items[0]
            return ProviderResult(True, o["id"], o.get("status", "created"), o["amount"], "order", o)
        d = self._request("GET", "/payment_links", params={"reference_id": idempotency_key[:40],
                                                           "count": 1})
        items = d.get("payment_links") or d.get("items") or []
        if items:
            p = items[0]
            return ProviderResult(True, p["id"], p.get("status", "created"), p["amount"],
                                  "payment_link", p, short_url=p.get("short_url"))
        return None

    def close(self) -> None:
        self._client.close()


def _safe_json(r: httpx.Response) -> dict[str, Any]:
    try:
        d = r.json()
        return d if isinstance(d, dict) else {"data": d}
    except ValueError:
        return {"raw_text": r.text[:500]}
