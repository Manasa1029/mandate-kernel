"""HTTP surface for the kernel. FastAPI, sync handlers on purpose.

Why sync: every handler ends in a SQLite `BEGIN IMMEDIATE` transaction. Making
them `async def` would put a blocking write lock inside the event loop and turn a
correctness property into a latency bug. FastAPI runs `def` handlers in a
threadpool, and SQLite serialises writers for us.

Routes
    POST /v1/mandates/intent      demo stand-in for the user's signing device
    POST /v1/evaluate             run the 8 gates (state-changing: reserves + claims)
    POST /v1/execute              redeem a capability and call the provider
    POST /v1/pay                  evaluate + execute in one call (what agents use)
    POST /v1/compensate           saga rollback for a post-condition failure
    GET  /v1/mandates/{id}/state  spend/velocity state
    POST /v1/mandates/{id}/revoke kill one mandate
    GET  /v1/trace/{action_id}    ordered ledger entries for one action
    GET  /v1/ledger               recent entries
    GET  /v1/ledger/verify        hash-chain integrity proof
    POST /v1/webhooks/razorpay    HMAC-verified provider callbacks (never trusted blind)
    POST /v1/admin/kill-switch    stop the world (refunds still allowed)
    GET  /v1/keys                 public key ids the kernel trusts
    GET  /healthz
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from adapters import build_provider
from bootstrap import IDENTITIES
from kernel.config import KernelConfig
from kernel.crypto import sign_payload
from kernel.executor import Executor
from kernel.models import (
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

log = logging.getLogger("api")

CFG = KernelConfig.from_env()
STORE = Store(CFG.db_path)
PROVIDER = build_provider(CFG.razorpay_mode, timeout=CFG.provider_timeout_s)
KERNEL = Kernel(STORE, IDENTITIES.registry, CFG)
EXECUTOR = Executor(STORE, PROVIDER, CFG)

# Capability token -> verdict, so /v1/execute can be a separate call from
# /v1/evaluate. In production this is a Redis entry with the capability's TTL;
# it is intentionally NOT the source of truth (the DB row is).
_PENDING: dict[str, tuple[Verdict, ProposedAction]] = {}

app = FastAPI(title="Mandate Kernel", version="1.0.0",
              description="Deterministic policy kernel for agent-initiated payments.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------- schemas

class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_playback: StrictStr
    constraints: Constraints
    ttl_seconds: StrictInt = 3600
    human_present: StrictBool = True
    subject: StrictStr | None = None


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability_token: StrictStr


class CompensateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mandate_id: StrictStr
    payment_id: StrictStr
    amount_paise: StrictInt
    cause: StrictStr


# ---------------------------------------------------------------------- routes

@app.get("/healthz")
def healthz() -> dict[str, Any]:
    ok, bad, msg = STORE.verify_chain()
    return {"ok": True, "provider": PROVIDER.name, "ledger_intact": ok, "ledger_note": msg,
            "bad_seq": bad, "kill_switch": STORE.flag_get("kill_switch", "0") == "1",
            "db": CFG.db_path}


@app.get("/v1/keys")
def keys() -> dict[str, Any]:
    ids = IDENTITIES
    return {"user": ids.user.key_id, "agent_delegated": ids.agent.key_id,
            "agent_rogue_registered_but_not_delegated": ids.rogue.key_id,
            "merchant": ids.merchant.key_id, "alg": "Ed25519"}


@app.post("/v1/mandates/intent")
def create_intent(req: IntentRequest) -> dict[str, Any]:
    """Stand-in for the user's device. In production the private key never leaves
    the phone's secure element and this endpoint does not exist."""
    t = now_s()
    intent = IntentMandate(
        mandate_id=new_id("mnd"), subject=req.subject or IDENTITIES.user.subject,
        delegated_agents=(IDENTITIES.agent.key_id,), human_present=req.human_present,
        prompt_playback=req.prompt_playback, constraints=req.constraints,
        issued_at=t, expires_at=t + max(60, min(req.ttl_seconds, 30 * 24 * 3600)),
        nonce=new_id("n"))
    envelope = sign_payload(IDENTITIES.user, intent.signable())
    STORE.append("mandate.issued", {"mandate_id": intent.mandate_id,
                                    "prompt_playback": intent.prompt_playback,
                                    "constraints": intent.constraints.model_dump(mode="json"),
                                    "expires_at": intent.expires_at},
                 mandate_id=intent.mandate_id)
    return {"intent": envelope, "mandate_id": intent.mandate_id}


@app.post("/v1/evaluate")
def evaluate(request: KernelRequest) -> JSONResponse:
    verdict = KERNEL.evaluate(request)
    if verdict.allowed and verdict.capability is not None:
        action = ProposedAction.model_validate(request.action.payload)
        _PENDING[verdict.capability.token] = (verdict, action)
    return JSONResponse(status_code=200 if verdict.allowed else 403,
                        content=verdict.model_dump(mode="json"))


@app.post("/v1/execute")
def execute(req: ExecuteRequest) -> JSONResponse:
    pending = _PENDING.pop(req.capability_token, None)
    if pending is None:
        raise HTTPException(409, "unknown or already-consumed capability token")
    verdict, action = pending
    outcome = EXECUTOR.execute(verdict, action)
    return JSONResponse(status_code=200 if outcome.succeeded else 402,
                        content=_outcome_json(outcome, verdict))


@app.post("/v1/pay")
def pay(request: KernelRequest) -> JSONResponse:
    """One call: gate it, then execute it. The agent's normal path."""
    verdict = KERNEL.evaluate(request)
    body: dict[str, Any] = {"verdict": verdict.model_dump(mode="json")}
    if not verdict.allowed:
        body["execution"] = None
        # 409 for "already done, here is the original result"; 403 for a policy denial.
        code = 409 if verdict.replayed_result is not None else 403
        return JSONResponse(status_code=code, content=body)

    action = ProposedAction.model_validate(request.action.payload)
    outcome = EXECUTOR.execute(verdict, action)
    body["execution"] = _outcome_json(outcome, verdict)["execution"]
    return JSONResponse(status_code=200 if outcome.succeeded else 402, content=body)


@app.post("/v1/compensate")
def compensate(req: CompensateRequest) -> JSONResponse:
    outcome = EXECUTOR.compensate(mandate_id=req.mandate_id, payment_id=req.payment_id,
                                 amount_paise=req.amount_paise, cause=req.cause)
    return JSONResponse(status_code=200 if outcome.state == "compensated" else 502,
                        content=_outcome_json(outcome, None))


@app.get("/v1/mandates/{mandate_id}/state")
def mandate_state(mandate_id: str) -> dict[str, Any]:
    """Live spend/velocity state. Headroom is derived from the mandate's own
    constraints as recorded at issue time, so this endpoint stays correct even
    though the kernel deliberately keeps no mutable copy of the mandate."""
    issued = next((e for e in STORE.trace_mandate(mandate_id)
                   if e["kind"] == "mandate.issued"), None)
    if issued is None:
        # Deliberately a 404 rather than an all-zero body: answering "0 spent, not
        # revoked" for a mandate that was never issued is the kind of confidently
        # wrong reply that hides a typo'd id until it matters.
        raise HTTPException(404, "unknown mandate_id")
    state = STORE.spend_state(mandate_id)
    max_total = issued["payload"]["constraints"]["max_total_paise"]
    headroom = max(0, max_total - state["committed"] - state["reserved"])
    return {"mandate_id": mandate_id, "committed_paise": state["committed"],
            "reserved_paise": state["reserved"], "txn_count": state["txn_count"],
            "denial_streak": state["denial_streak"], "breaker_until": state["breaker_until"],
            "revoked": bool(state["revoked"]), "headroom_paise": headroom}


@app.post("/v1/mandates/{mandate_id}/revoke")
def revoke(mandate_id: str, reason: str = Body("user_requested", embed=True)) -> dict[str, Any]:
    STORE.revoke_mandate(mandate_id)
    seq, _ = STORE.append("mandate.revoked", {"mandate_id": mandate_id, "reason": reason},
                          mandate_id=mandate_id)
    return {"revoked": True, "mandate_id": mandate_id, "ledger_seq": seq}


@app.get("/v1/trace/{action_id}")
def trace(action_id: str) -> dict[str, Any]:
    entries = STORE.trace(action_id)
    if not entries:
        raise HTTPException(404, "no ledger entries for that action_id")
    return {"action_id": action_id, "entries": entries}


@app.get("/v1/ledger")
def ledger(limit: int = 50) -> dict[str, Any]:
    # Clamp both ends. SQLite treats a negative LIMIT as unlimited, so `min(limit, 500)`
    # alone let `?limit=-1` dump the entire ledger in one request.
    return {"entries": STORE.recent(max(1, min(limit, 500)))}


@app.get("/v1/ledger/verify")
def ledger_verify() -> dict[str, Any]:
    ok, bad, msg = STORE.verify_chain()
    return {"intact": ok, "first_bad_seq": bad, "message": msg}


@app.post("/v1/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> JSONResponse:
    """Provider callbacks. Logged either way, believed only if the HMAC verifies.

    Three properties worth naming:
      * The RAW body is verified before it is parsed. Parsing first and
        re-serialising changes the bytes and breaks the signature.
      * A failed verification is still written to the ledger, as
        `webhook.rejected`. Silently dropping forged callbacks throws away the
        only evidence that someone is probing you.
      * The webhook is treated as a *hint to reconcile*, never as truth about
        money. It never credits or debits budget directly — the executor's
        reconciliation path does that after asking the provider.
    """
    from adapters.razorpay_rest import verify_webhook

    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    ok = verify_webhook(body, signature)

    try:
        event = json.loads(body or b"{}")
        event_name = str(event.get("event", "unknown"))
    except json.JSONDecodeError:
        event, event_name = {}, "unparseable"

    STORE.append("webhook.accepted" if ok else "webhook.rejected",
                 {"event": event_name, "verified": ok,
                  "signature_present": bool(signature),
                  "payload_bytes": len(body),
                  "entity_ids": sorted({v for k, v in _flat_ids(event)})[:10]})
    if not ok:
        raise HTTPException(400, "webhook signature verification failed")
    return JSONResponse(status_code=200, content={"received": True, "event": event_name,
                                                 "note": "logged; reconciliation is pull-based"})


def _flat_ids(obj: Any, out: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """Collect provider ids (order_/pay_/rfnd_) from a nested webhook body."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "id" and isinstance(v, str) and v.split("_")[0] in {"order", "pay", "rfnd", "plink"}:
                out.append((k, v))
            else:
                _flat_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flat_ids(v, out)
    return out


@app.post("/v1/admin/kill-switch")
def kill_switch(on: bool = Body(..., embed=True),
                reason: str = Body("operator", embed=True)) -> dict[str, Any]:
    STORE.flag_set("kill_switch", "1" if on else "0")
    seq, _ = STORE.append("admin.kill_switch", {"on": on, "reason": reason})
    return {"kill_switch": on, "ledger_seq": seq}


# ---------------------------------------------------------------------- helper

def _outcome_json(outcome, verdict: Verdict | None) -> dict[str, Any]:
    return {
        "execution": {
            "state": outcome.state, "reason": outcome.reason, "provider_id": outcome.provider_id,
            "provider_status": outcome.provider_status, "attempts": outcome.attempts,
            "requires_human": outcome.requires_human,
            "escalation_advised": outcome.escalation_advised,
            "compensation": outcome.compensation, "ledger_seqs": outcome.ledger_seqs,
            "detail": outcome.raw,
        },
        "verdict": verdict.model_dump(mode="json") if verdict is not None else None,
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
