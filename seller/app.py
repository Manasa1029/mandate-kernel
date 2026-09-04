"""Seller surface: agent-discoverable catalogue + merchant-signed quotes.

This is the counterparty in the demo, and it is deliberately *not* trusted:
  * It signs its quotes (so the price cannot drift after the fact), but the kernel
    still recomputes the line math — a signature proves origin, not correctness.
  * It serves hostile listings verbatim.
  * It can fail fulfilment *after* money moved, which is what triggers the saga.

Endpoints
    GET  /.well-known/agent-catalog.json   discovery document for agents
    GET  /search?q=&category=              product search (returns raw text)
    POST /quote                            -> merchant-signed CartMandate envelope
    POST /fulfil                           -> fulfilment result (can fail on purpose)
    GET  /healthz
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from bootstrap import IDENTITIES, MERCHANT_ID, MERCHANT_PAYEE
from kernel.crypto import sign_payload
from kernel.models import CartItem, CartMandate, new_id, now_s

from . import catalog

QUOTE_TTL_S = int(os.environ.get("QUOTE_TTL_S", "300"))
SHIPPING_PAISE = int(os.environ.get("SHIPPING_PAISE", "4000"))
FREE_SHIP_ABOVE = int(os.environ.get("FREE_SHIP_ABOVE_PAISE", "50000"))
# SKUs whose fulfilment fails after payment — drives the compensation demo.
FULFIL_FAIL_SKUS = set(filter(None, os.environ.get("FULFIL_FAIL_SKUS", "SKU-GHEE-BULK").split(",")))

app = FastAPI(title="Acme Pantry (agent-facing seller)", version="1.0.0")

_ISSUED_QUOTES: dict[str, dict[str, Any]] = {}


class QuoteLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: StrictStr
    qty: StrictInt = Field(gt=0, le=50)


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[QuoteLine] = Field(min_length=1, max_length=25)
    intent_ref: StrictStr | None = None


class FulfilRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cart_id: StrictStr
    provider_payment_id: StrictStr | None = None


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "merchant_id": MERCHANT_ID, "skus": len(catalog.CATALOG)}


@app.get("/.well-known/agent-catalog.json")
def discovery() -> dict[str, Any]:
    """Minimal agent-discovery document, in the spirit of ACP/AP2 merchant manifests."""
    return {
        "merchant_id": MERCHANT_ID,
        "display_name": "Acme Pantry",
        "payee": MERCHANT_PAYEE,
        "currency": "INR",
        "signing_key_id": IDENTITIES.merchant.key_id,
        "categories": list(catalog.CATEGORIES),
        "quote_endpoint": "/quote",
        "search_endpoint": "/search",
        "quote_ttl_seconds": QUOTE_TTL_S,
        "mandate_profile": "ap2-like/1",
        "notes": "Quotes are Ed25519-signed. Amounts are integer paise. Prices are firm "
                 "only until price_valid_until.",
    }


@app.get("/search")
def search(q: str = "", category: str | None = None, limit: int = 20) -> dict[str, Any]:
    hits = catalog.search(q, category, min(limit, 50))
    return {"count": len(hits), "items": [catalog.public_view(p) for p in hits]}


@app.post("/quote")
def quote(req: QuoteRequest) -> dict[str, Any]:
    lines: list[CartItem] = []
    subtotal = 0
    tax = 0
    seen: set[str] = set()

    for line in req.items:
        p = catalog.BY_SKU.get(line.sku)
        if p is None:
            raise HTTPException(404, f"unknown sku {line.sku}")
        if line.sku in seen:
            raise HTTPException(400, f"duplicate sku {line.sku}: merge quantities before quoting")
        seen.add(line.sku)
        gross = p.price_paise * line.qty
        line_tax = gross * p.tax_bp // 10_000  # integer, floor — matches invoice rounding
        subtotal += gross
        tax += line_tax
        lines.append(CartItem(sku=p.sku, name=p.name, category=p.category, qty=line.qty,
                              unit_price_paise=p.price_paise, tax_paise=line_tax))

    shipping = 0 if subtotal >= FREE_SHIP_ABOVE else SHIPPING_PAISE
    t = now_s()
    cart = CartMandate(
        cart_id=new_id("cart"), merchant_id=MERCHANT_ID, intent_ref=req.intent_ref,
        items=tuple(lines), subtotal_paise=subtotal, tax_paise=tax, shipping_paise=shipping,
        total_paise=subtotal + tax + shipping, payee=MERCHANT_PAYEE,
        quoted_at=t, price_valid_until=t + QUOTE_TTL_S, nonce=new_id("n"),
    )
    envelope = sign_payload(IDENTITIES.merchant, cart.signable())
    _ISSUED_QUOTES[cart.cart_id] = {"cart": cart.signable(), "skus": [i.sku for i in lines]}
    return {
        "cart": envelope,
        "display_total": f"₹{cart.total_paise / 100:,.2f}",
        "expires_in_seconds": QUOTE_TTL_S,
    }


@app.post("/fulfil")
def fulfil(req: FulfilRequest) -> dict[str, Any]:
    """Post-payment fulfilment. Failure here is what a saga is for."""
    record = _ISSUED_QUOTES.get(req.cart_id)
    if record is None:
        raise HTTPException(404, "unknown cart")
    blocked = [s for s in record["skus"] if s in FULFIL_FAIL_SKUS]
    if blocked:
        return {"fulfilled": False, "reason": "out_of_stock", "skus": blocked,
                "advice": "refund_required"}
    return {"fulfilled": True, "shipment_id": new_id("shp"), "eta_days": 2}
