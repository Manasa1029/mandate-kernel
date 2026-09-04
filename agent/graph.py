"""LangGraph buyer agent.

Why LangGraph rather than an agent loop: the money step must be a *named node*
with an explicit interrupt in front of it. `interrupt_before=["gate"]` is a
structural guarantee that the graph cannot reach the kernel without the run being
paused for a human first — and because LangGraph checkpoints state, that pause
survives the process dying. A ReAct loop can be prompted to ask permission; a
graph edge cannot be talked out of it.

Flow

    search ──> plan ──> quote ──> approve ──┤interrupt├──> gate
                                                            │
                              deny ─────────────────────────>│
                                                            ▼
                                                         execute
                              ┌───────────┬────────────┬────────┐
                              ▼           ▼            ▼        ▼
                           fulfil     escalate     freeze    stopped
                              │           │
                    ok ───────┴──> END    └──> gate (new idempotency key)
                    fail ──> compensate ──> END

Every terminal state is named, because "what does your agent do when it fails" is
a question with a real answer here.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph

from bootstrap import MERCHANT_ID
from kernel.models import ActionKind, AttemptClass, Envelope, IntentMandate

from . import tools
from .planner import DeterministicPlanner, Plan, build_planner
from .prompts import APPROVAL_TEMPLATE

log = logging.getLogger("agent")

MAX_ESCALATIONS = 1


def _append(a: list[str], b: list[str]) -> list[str]:
    return (a or []) + (b or [])


class AgentState(TypedDict, total=False):
    goal: str
    intent: Any
    intent_env: Any
    catalogue: list[dict[str, Any]]
    plan: dict[str, Any]
    cart: Any
    cart_env: Any
    action: Any
    verdict: dict[str, Any]
    capability_token: str | None
    approval_prompt: str
    approved: bool
    outcome: dict[str, Any]
    fulfilment: dict[str, Any]
    compensation: dict[str, Any]
    terminal: str
    attempt: int
    escalations: int
    trail: Annotated[list[str], _append]


def build_graph(rt: tools.Runtime, planner=None, *, auto_approve: bool = False):
    planner = planner or build_planner()

    # ---------------------------------------------------------------- nodes

    def search(state: AgentState) -> AgentState:
        # Deliberately unfiltered and generous: the hostile listings live at the
        # end of the catalogue, and truncating them away would be cheating.
        items = tools.search_catalog(query="", category=None, limit=100)
        return {"catalogue": items, "attempt": 1, "escalations": 0,
                "trail": [f"search: {len(items)} listings retrieved (untrusted text)"]}

    def plan(state: AgentState) -> AgentState:
        intent: IntentMandate = state["intent"]
        c = intent.constraints
        st = tools.mandate_state(rt, intent.mandate_id)
        p: Plan = planner.plan(
            goal=state["goal"], catalogue=state["catalogue"], playback=intent.prompt_playback,
            max_total_paise=c.max_total_paise - st["committed"] - st["reserved"],
            max_per_txn_paise=c.max_per_txn_paise,
            slots=max(0, c.max_transactions - st["txn_count"]),
            categories=c.allowed_categories, merchants=c.allowed_merchants)
        rt.store.append("agent.plan", {"planner": p.planner, "items": p.items,
                                       "flagged": p.flagged, "reasoning": p.reasoning},
                        mandate_id=intent.mandate_id)
        return {"plan": {"items": p.items, "flagged": p.flagged, "reasoning": p.reasoning,
                         "planner": p.planner},
                "trail": [f"plan[{p.planner}]: {len(p.items)} items, {len(p.flagged)} listing(s) flagged"]}

    def quote(state: AgentState) -> AgentState:
        items = (state.get("plan") or {}).get("items") or []
        if not items:
            return {"terminal": "no_plan", "trail": ["quote: nothing in scope to buy — stopping"]}
        try:
            cart, cart_env = tools.get_quote(rt, items, state["intent"].mandate_id)
        except ValueError as e:
            return {"terminal": "quote_failed", "trail": [f"quote: rejected by seller — {e}"]}
        action = tools.build_action(state["intent"], cart,
                                   rationale=(state.get("plan") or {}).get("reasoning", ""))
        return {"cart": cart, "cart_env": cart_env, "action": action,
                "trail": [f"quote: cart {cart.cart_id} total {cart.total_paise} paise, "
                          f"valid {cart.price_valid_until - cart.quoted_at}s"]}

    def approve(state: AgentState) -> AgentState:
        """Renders the approval prompt. The interrupt is configured on `gate`, so
        the graph pauses immediately after this node with the prompt in state."""
        cart, intent = state["cart"], state["intent"]
        st = tools.mandate_state(rt, intent.mandate_id)
        prompt = APPROVAL_TEMPLATE.format(
            amount_rupees=f"{cart.total_paise / 100:,.2f}", payee=cart.payee, merchant=MERCHANT_ID,
            summary=", ".join(f"{i.qty}x {i.name}" for i in cart.items)[:160],
            mandate_id=intent.mandate_id,
            per_txn_rupees=f"{intent.constraints.max_per_txn_paise / 100:,.2f}",
            headroom_rupees=f"{(intent.constraints.max_total_paise - st['committed'] - st['reserved']) / 100:,.2f}",
            quote_seconds=cart.price_valid_until - cart.quoted_at)
        return {"approval_prompt": prompt, "approved": bool(auto_approve),
                "trail": ["approve: awaiting human confirmation" if not auto_approve
                          else "approve: auto-approved (demo mode)"]}

    def gate(state: AgentState) -> AgentState:
        if not state.get("approved"):
            return {"terminal": "declined_by_human",
                    "trail": ["gate: human declined — no proposal submitted"]}
        verdict = tools.propose_payment(rt, intent_env=state["intent_env"],
                                        cart_env=state["cart_env"], action=state["action"])
        token = verdict.capability.token if verdict.capability else None
        reasons = [f"{g.ordinal}:{g.gate}={g.decision.value}" for g in verdict.gates]
        return {"verdict": verdict.model_dump(mode="json"), "capability_token": token,
                "trail": [f"gate: {verdict.decision.value} ({verdict.reason}) [{' '.join(reasons)}]"]}

    def execute(state: AgentState) -> AgentState:
        outcome = tools.execute_capability(rt, state["capability_token"] or "")
        return {"outcome": {"state": outcome.state, "reason": outcome.reason,
                            "provider_id": outcome.provider_id, "attempts": outcome.attempts,
                            "requires_human": outcome.requires_human,
                            "escalation_advised": outcome.escalation_advised,
                            "detail": outcome.raw},
                "trail": [f"execute: {outcome.state} ({outcome.reason}) "
                          f"after {outcome.attempts} attempt(s)"]}

    def escalate(state: AgentState) -> AgentState:
        """Same cart, different instrument. New attempt number -> new idempotency
        key, and the kernel re-runs all eight gates from scratch."""
        attempt = int(state.get("attempt", 1)) + 1
        action = tools.build_action(state["intent"], state["cart"], attempt=attempt,
                                   attempt_class=AttemptClass.ESCALATION,
                                   action=ActionKind.CREATE_PAYMENT_LINK,
                                   rationale="instrument escalation after provider decline")
        return {"action": action, "attempt": attempt,
                "escalations": int(state.get("escalations", 0)) + 1, "approved": True,
                "trail": [f"escalate: attempt {attempt} as payment_link (new idempotency key)"]}

    def fulfil(state: AgentState) -> AgentState:
        res = tools.fulfil(rt, state["cart"].cart_id)
        out: AgentState = {"fulfilment": res,
                           "trail": [f"fulfil: {'ok' if res.get('fulfilled') else res.get('reason')}"]}
        if res.get("fulfilled"):
            # Name the happy path too. A run that ends in `stopped` is indistinguishable
            # from a run that gave up, which makes the ledger harder to audit.
            out["terminal"] = "fulfilled"
        return out

    def compensate(state: AgentState) -> AgentState:
        """Money moved, goods will not. Refund, log, stop."""
        order_id = (state.get("outcome") or {}).get("provider_id")
        pay = tools.simulate_customer_payment(rt, order_id, authorize_only=False)
        out = rt.executor.compensate(mandate_id=state["intent"].mandate_id,
                                     payment_id=pay.get("payment_id", ""),
                                     amount_paise=state["cart"].total_paise,
                                     cause=(state.get("fulfilment") or {}).get("reason", "unknown"),
                                     action_id=state["action"].action_id)
        return {"compensation": {"state": out.state, "refund_id": out.provider_id,
                                 "detail": out.compensation},
                "terminal": "compensated",
                "trail": [f"compensate: {out.state} refund {out.provider_id}"]}

    def freeze(state: AgentState) -> AgentState:
        return {"terminal": "frozen_unknown_state",
                "trail": ["freeze: provider state unknown — budget stays reserved, "
                          "kill switch engaged, human required"]}

    def stop(state: AgentState) -> AgentState:
        return {"terminal": state.get("terminal") or "stopped",
                "trail": ["stop: terminal state reached"]}

    # ----------------------------------------------------------------- edges

    def after_quote(state: AgentState) -> Literal["approve", "stop"]:
        return "stop" if state.get("terminal") else "approve"

    def after_gate(state: AgentState) -> Literal["execute", "stop"]:
        v = state.get("verdict") or {}
        return "execute" if v.get("decision") == "allow" else "stop"

    def after_execute(state: AgentState) -> Literal["fulfil", "escalate", "freeze", "stop"]:
        o = state.get("outcome") or {}
        if o.get("state") == "done":
            return "fulfil"
        if o.get("state") == "unknown":
            return "freeze"
        if o.get("escalation_advised") and int(state.get("escalations", 0)) < MAX_ESCALATIONS:
            return "escalate"
        return "stop"

    def after_fulfil(state: AgentState) -> Literal["compensate", "stop"]:
        return "stop" if (state.get("fulfilment") or {}).get("fulfilled") else "compensate"

    g = StateGraph(AgentState)
    for name, fn in (("search", search), ("plan", plan), ("quote", quote), ("approve", approve),
                     ("gate", gate), ("execute", execute), ("escalate", escalate),
                     ("fulfil", fulfil), ("compensate", compensate), ("freeze", freeze),
                     ("stop", stop)):
        g.add_node(name, fn)

    g.set_entry_point("search")
    g.add_edge("search", "plan")
    g.add_edge("plan", "quote")
    g.add_conditional_edges("quote", after_quote, {"approve": "approve", "stop": "stop"})
    g.add_edge("approve", "gate")
    g.add_conditional_edges("gate", after_gate, {"execute": "execute", "stop": "stop"})
    g.add_conditional_edges("execute", after_execute,
                            {"fulfil": "fulfil", "escalate": "escalate", "freeze": "freeze",
                             "stop": "stop"})
    g.add_edge("escalate", "gate")
    g.add_conditional_edges("fulfil", after_fulfil, {"compensate": "compensate", "stop": "stop"})
    g.add_edge("compensate", END)
    g.add_edge("freeze", END)
    g.add_edge("stop", END)

    # The interrupt sits in front of `gate`: the graph physically cannot submit a
    # proposal without the caller resuming the run.
    # Our mandates/carts are pydantic models, so the checkpointer needs them on the
    # allow-list; otherwise LangGraph warns now and refuses to deserialise later.
    serde = JsonPlusSerializer(allowed_msgpack_modules=[
        ("kernel.models", name) for name in (
            "IntentMandate", "CartMandate", "CartItem", "Constraints", "ProposedAction",
            "Envelope", "ActionKind", "AttemptClass", "Capability", "Verdict", "GateResult",
            "Decision",
        )
    ])
    return g.compile(checkpointer=MemorySaver(serde=serde),
                     interrupt_before=[] if auto_approve else ["gate"])


def run_once(rt: tools.Runtime, *, goal: str, intent, intent_env: Envelope,
             approve: bool = True, planner=None, thread_id: str = "demo") -> AgentState:
    """Drive the graph through its interrupt. Returns the final state.

    Mirrors what a UI does: start the run, read the approval prompt from the
    checkpoint, write the human's answer into state, resume.
    """
    graph = build_graph(rt, planner=planner or DeterministicPlanner())
    cfg = {"configurable": {"thread_id": thread_id}}
    state: dict[str, Any] = {"goal": goal, "intent": intent, "intent_env": intent_env}
    graph.invoke(state, cfg)

    snapshot = graph.get_state(cfg)
    if snapshot.next:  # paused before `gate`
        graph.update_state(cfg, {"approved": bool(approve),
                                 "trail": [f"human: {'approved' if approve else 'declined'}"]})
        graph.invoke(None, cfg)
        snapshot = graph.get_state(cfg)
    return snapshot.values  # type: ignore[return-value]
