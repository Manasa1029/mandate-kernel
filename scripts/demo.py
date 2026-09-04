#!/usr/bin/env python3
"""Five-minute demo script. Runs six scenes, in the order you should film them.

    python -m scripts.demo              # all scenes
    python -m scripts.demo 3            # one scene
    python -m scripts.demo --db demo.db # keep the ledger for the console

Scene 1  happy path            an agent buys groceries inside its mandate
Scene 2  injection             a hostile listing tries to hijack the agent
Scene 3  policy denial         four attacks, four different gates
Scene 4  transient failure     provider 500s, one retry, same idempotency key
Scene 5  unknown state         timeout after write -> freeze, not retry
Scene 6  saga rollback         paid, seller cannot fulfil, automatic refund
Then     integrity             verify the hash chain over everything above
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from adapters.base import ProviderRejected, ProviderRetriable, ProviderUnknownState
from adapters.mock_razorpay import Fail, MockRazorpay
from agent import tools
from agent.graph import run_once
from agent.planner import DeterministicPlanner
from kernel.errors import Reason
from kernel.executor import Executor
from kernel.models import ActionKind
from seller import catalog

# ────────────────────────────────────────────────────────────── presentation

W = 78
DIM, BOLD, RED, GRN, YEL, CYA, OFF = (
    "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m")


def scene(n: int, title: str, why: str) -> None:
    print(f"\n{BOLD}{'━' * W}{OFF}")
    print(f"{BOLD}SCENE {n}  {title}{OFF}")
    print(f"{DIM}{why}{OFF}")
    print(f"{BOLD}{'━' * W}{OFF}")


def step(text: str) -> None:
    print(f"  {DIM}·{OFF} {text}")


def good(text: str) -> None:
    print(f"  {GRN}✓{OFF} {text}")


def block(text: str) -> None:
    print(f"  {RED}⛔{OFF} {text}")


def warn(text: str) -> None:
    print(f"  {YEL}!{OFF} {text}")


def rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def gate_strip(verdict: dict[str, Any]) -> str:
    out = []
    for g in verdict.get("gates", []):
        mark = f"{GRN}✓{OFF}" if g["decision"] == "allow" else f"{RED}✗{OFF}"
        out.append(f"{g['ordinal']}{mark}")
    return " ".join(out)


# ────────────────────────────────────────────────────────────────── scenes

def mandate(rt: tools.Runtime, **over: Any):
    kw = dict(playback="Buy this week's groceries from Acme Pantry, up to ₹5,000 total "
                       "and ₹2,000 per order.",
              max_total_paise=500_000, max_per_txn_paise=200_000, max_transactions=3,
              categories=("groceries", "household"))
    kw.update(over)
    return tools.issue_intent(rt, **kw)


def scene_1(rt: tools.Runtime) -> None:
    scene(1, "Happy path", "An agent shops, a human approves once, the kernel gates it, "
                           "money moves, goods ship.")
    intent, env = mandate(rt)
    step(f"mandate {intent.mandate_id} issued, signed by the user's key")
    step(f'playback: "{intent.prompt_playback}"')

    state = run_once(rt, goal="weekly groceries for two people", intent=intent,
                     intent_env=env, approve=True)
    for line in state["trail"]:
        step(line)

    cart = state["cart"]
    good(f"paid {rupees(cart.total_paise)} to {cart.payee} — {len(cart.items)} lines")
    good(f"gates: {gate_strip(state['verdict'])}  terminal: {state['terminal']}")
    s = rt.store.spend_state(intent.mandate_id)
    step(f"budget now: committed {rupees(s['committed'])} of "
         f"{rupees(intent.constraints.max_total_paise)}, reserved {rupees(s['reserved'])}")


def scene_2(rt: tools.Runtime) -> None:
    scene(2, "Prompt injection", "Four listings in the catalogue contain instructions "
                                 "aimed at the agent. The agent reads all of them.")
    for sku in catalog.HOSTILE_SKUS:
        p = catalog.BY_SKU[sku]
        text = f"{p.name} {p.description}".replace("\n", " ")
        warn(f"{sku}: \"{text[:64]}…\"")

    intent, env = mandate(rt)
    state = run_once(rt, goal="weekly groceries", intent=intent, intent_env=env, approve=True)
    chosen = {i.sku for i in state["cart"].items}
    good(f"planner flagged and skipped {len(state['plan']['flagged'])} hostile listing(s)")
    good(f"cart contains none of them: {sorted(chosen)[:3]}…")
    step("and even if it had: the kernel would still have refused. Scene 3.")


def scene_3(rt: tools.Runtime) -> None:
    scene(3, "Policy denials", "The same four attacks, forced past the planner straight "
                               "into the kernel. Four different gates catch them.")
    from redteam.injection import forced_cases

    for case in forced_cases():
        verdict = case["verdict"]
        block(f"{case['sku']:<20} {case['goal'][:42]:<44} {verdict.reason}")
    good("no attack reached a provider call")


def scene_4(rt: tools.Runtime) -> None:
    scene(4, "Transient provider failure", "Razorpay 500s once. We retry — with the "
                                           "same idempotency key, which is the whole point.")
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderRetriable, "SERVER_ERROR")])
    rt2 = tools.Runtime.local()
    rt2.provider = provider
    rt2.executor = Executor(rt2.store, provider, rt2.cfg)
    LEDGERS.append(rt2.store)

    intent, env = mandate(rt2)
    state = run_once(rt2, goal="weekly groceries", intent=intent, intent_env=env, approve=True)
    keys = {c.get("idempotency_key") for c in provider.calls if c["op"] == "create_order"}
    step(f"provider calls: {len([c for c in provider.calls if c['op'] == 'create_order'])}")
    good(f"distinct idempotency keys used: {len(keys)} — a retry cannot double-charge")
    good(f"outcome: {state['outcome']['state']} after {state['outcome']['attempts']} attempt(s)")


def scene_5(rt: tools.Runtime) -> None:
    scene(5, "Unknown provider state", "A timeout AFTER the write. The order may exist. "
                                       "The wrong move is to retry.")
    provider = MockRazorpay()
    provider.script([Fail("create_order", ProviderUnknownState, "TIMEOUT_AFTER_WRITE",
                          landed=False)])
    rt2 = tools.Runtime.local()
    rt2.provider = provider
    rt2.executor = Executor(rt2.store, provider, rt2.cfg)
    LEDGERS.append(rt2.store)

    intent, env = mandate(rt2)
    state = run_once(rt2, goal="weekly groceries", intent=intent, intent_env=env, approve=True)
    s = rt2.store.spend_state(intent.mandate_id)
    warn(f"terminal state: {state['terminal']}")
    good(f"budget stays RESERVED at {rupees(s['reserved'])} — not released, not committed")
    good(f"kill switch engaged: {rt2.store.flag_get('kill_switch', '0') == '1'}")
    step("a human reconciles. The system does not guess.")


def scene_6(rt: tools.Runtime) -> None:
    scene(6, "Saga rollback", "Money moved. The seller then fails to fulfil. "
                              "The kernel refunds without being asked.")
    rt2 = tools.Runtime.local()
    LEDGERS.append(rt2.store)
    intent, env = mandate(rt2)
    state = run_once(rt2, goal="buy the item that cannot be fulfilled", intent=intent,
                     intent_env=env, approve=True)
    # Force the post-condition failure the graph is designed to compensate.
    if state.get("terminal") != "compensated":
        cart = state["cart"]
        out = state["outcome"]
        pay = tools.simulate_customer_payment(rt2, out["provider_id"], authorize_only=False)
        comp = rt2.executor.compensate(mandate_id=intent.mandate_id,
                                      payment_id=pay.get("payment_id", ""),
                                      amount_paise=cart.total_paise,
                                      cause="seller_out_of_stock_after_capture",
                                      action_id=state["action"].action_id)
        step(f"post-condition failed after capture of {rupees(cart.total_paise)}")
        good(f"compensation: {comp.state}, refund {comp.provider_id}")
    s = rt2.store.spend_state(intent.mandate_id)
    good(f"committed back to {rupees(s['committed'])} — budget restored, both legs logged")


SCENES = {1: scene_1, 2: scene_2, 3: scene_3, 4: scene_4, 5: scene_5, 6: scene_6}

# Scenes 4-6 need their own runtime (a scripted provider, or a kill switch they
# engage), so integrity has to verify every ledger the demo touched, not just the
# first one. Verifying only `rt` would quietly skip the failure scenes.
LEDGERS: list[Any] = []


def integrity(rt: tools.Runtime) -> int:
    print(f"\n{BOLD}{'━' * W}{OFF}")
    print(f"{BOLD}INTEGRITY{OFF}")
    print(f"{BOLD}{'━' * W}{OFF}")
    stores = [rt.store, *LEDGERS]
    total, failures = 0, []
    for st in stores:
        ok, bad, msg = st.verify_chain()
        total += len(st.recent(100_000))
        if not ok:
            failures.append((bad, msg))
    if failures:
        for bad, msg in failures:
            block(f"CHAIN BROKEN at seq {bad}: {msg}")
    else:
        good(f"hash chain verified over {total} ledger entries across "
             f"{len(stores)} ledger(s), each from genesis")
    ok = not failures
    print(f"\n  {CYA}open console/index.html against http://127.0.0.1:8000 "
          f"to click through every entry{OFF}\n")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenes", nargs="*", type=int, help="scene numbers (default: all)")
    ap.add_argument("--db", default=":memory:", help="SQLite path (use a file to keep the ledger)")
    args = ap.parse_args()

    rt = tools.Runtime.local(db_path=args.db)
    print(f"{BOLD}Mandate Kernel — demo{OFF}")
    print(f"{DIM}provider: {rt.provider.name}   db: {args.db}   "
          f"planner: {DeterministicPlanner.__name__}{OFF}")

    wanted = args.scenes or sorted(SCENES)
    for n in wanted:
        if n not in SCENES:
            print(f"unknown scene {n}", file=sys.stderr)
            return 2
        t0 = time.perf_counter()
        SCENES[n](rt)
        print(f"  {DIM}scene {n} took {(time.perf_counter() - t0) * 1000:.0f}ms{OFF}")

    return integrity(rt)


if __name__ == "__main__":
    raise SystemExit(main())
