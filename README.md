# Mandate Kernel

**A deterministic policy kernel for agent-initiated payments.**
An LLM may *propose* a payment. It may never *make* one.

Razorpay AI Buildathon — Track 1 (AI Growth & Agentic Commerce), built with Track 2 evaluation rigour.

---

## The problem in one paragraph

Every agentic-commerce demo puts an LLM next to a payment API and hopes the prompt
holds. It doesn't. A product description can tell the model to raise its own
budget; a retry can charge twice; a timeout leaves nobody knowing whether money
moved. The interesting engineering problem is not "can an agent call the payments
API" — it's **what stops it when it is wrong, compromised, or unlucky**.

## The answer

Split the system at the credential boundary.

```
LLM planner                    Mandate Kernel                   Executor
──────────                     ──────────────                   ────────
reads untrusted catalogue      8 deterministic gates            holds the ONLY
proposes a shopping list  ──▶  no model, no network,       ──▶  provider credential
holds no credentials           no prompt to hijack              single-use capability
                               fixed order, first deny wins     token, burned on use
```

The planner is assumed compromised. The kernel doesn't read the catalogue, doesn't
read the prompt, and doesn't ask a model anything. It reads three signed objects
and a state table, and returns `allow` or a stable reason code. Every decision —
allow or deny — is appended to a hash-chained ledger.

## Why this is a growth lever, not just a safety net

A merchant cannot let an autonomous buyer agent spend against their catalogue
until someone can answer "what's the worst this agent can do to me" with a
number, not a prompt. Today that answer is "trust the LLM," which is why almost
no merchant actually opens itself to AI-buyer traffic. The kernel is what turns
that "no" into a bounded "yes": a merchant can plug into agentic commerce with
per-transaction caps, an allowlist, and an audit trail that make the worst case
small and provable — the precondition for saying yes to a channel at all, not a
tax on top of it. This repo demonstrates the buyer side of that boundary end to
end; the same eight gates and capability model are what a merchant-side
integration would sit behind to become transactable by an AI buyer in the first
place.

## Measured results

Reproducible from a clean clone, no API keys, no network:

| Metric | Value |
|---|---:|
| Adversarial cases | 133 (74 attacks / 59 benign) |
| Attack block rate | 100.00% |
| **False-positive rate** | **0.00%** |
| Reason accuracy (blocked for the *predicted* reason) | 100.00% |
| Kernel latency p50 / p95 | 1.23 ms / 3.21 ms |
| Prompt-injection containment | 100% (4/4 families) |
| Test suite | 178 passing |
| Audit chain | intact |

False-positive rate is reported first-class on purpose: a payment guard that
blocks everything scores 100% on attacks and is worthless.

```bash
make eval     # runs all three suites and regenerates docs/EVALUATION.md
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
make install-dev          # runtime + test deps
make eval                 # 178 tests, 133 adversarial cases, 4 injection families
make demo                 # six narrated scenes in your terminal
make api                  # kernel on :8000
make console              # audit console on :8080 — click through every decision
```

No API key is needed for anything above. The default planner is deterministic; the
default provider is an in-process Razorpay mock with scriptable failures.

## The six scenes

`make demo` runs these in order — it is also the storyboard for the pitch video.

| # | Scene | What it proves |
|---|---|---|
| 1 | Happy path | Agent shops, human approves once, 8 gates pass, money moves, goods ship |
| 2 | Prompt injection | Four hostile listings try to hijack the agent; cart is unaffected |
| 3 | Policy denials | Same four attacks forced past the planner — four *different* gates catch them |
| 4 | Transient failure | Provider 500s; one retry, **same** idempotency key, no double charge |
| 5 | Unknown state | Timeout after write → freeze, budget stays reserved, kill switch on, human required |
| 6 | Saga rollback | Paid, then seller can't fulfil → automatic refund, both legs in the ledger |

Then it verifies the hash chain over every ledger the demo touched.

## The eight gates

Fixed order, short-circuit on first denial. Order is a design decision, not an
accident — see ARCHITECTURE.md.

| # | Gate | Stops |
|---|---|---|
| 1 | schema | malformed proposals, unknown fields, float amounts, unsupported verbs |
| 2 | signature | forged mandates, undelegated agents, carts not bound to the intent |
| 3 | freshness | expired mandates, stale quotes, replayed nonces, revoked mandates |
| 4 | budget | per-transaction and cumulative caps, in integer paise |
| 5 | allowlist | wrong merchant, wrong payee/VPA, out-of-scope SKU or category, denylist |
| 6 | price binding | cart math, cart hash, amount ≠ signed quote, refunds beyond settled spend |
| 7 | velocity | transaction count, rate limit, circuit breaker, global kill switch |
| 8 | idempotency | double submits, in-flight duplicates, replayed results |

Every denial returns a stable machine-readable code (`G5_ALLOW_PAYEE_NOT_PERMITTED`,
`G4_BUDGET_TOTAL_EXCEEDED`, …) that the console, the eval runner and the agent all
switch on.

## Repository map

```
kernel/            the whole trust boundary — no LLM, no network, no provider SDK
  models.py        IntentMandate, CartMandate, ProposedAction, Verdict (AP2-shaped)
  canonical.py     deterministic JSON for signing; rejects floats outright
  crypto.py        Ed25519 sign/verify, key registry, delegation
  gates/           g1_schema … g8_idempotency, one file per gate
  pipeline.py      run the gates in one transaction, decide, record, mint
  capability.py    single-use tokens scoped to one amount, payee and verb, 90s TTL
  executor.py      the only code that calls a provider; retries, saga, freeze
  store.py         hash-chained ledger + spend/velocity/nonce/idempotency state
  api.py           FastAPI surface, including HMAC-verified webhooks
adapters/          mock Razorpay (scriptable failures) + real REST client
agent/             LangGraph buyer agent: search → plan → quote → approve → gate → execute
seller/            48-product catalogue (4 deliberately hostile), storefront, MCP server
redteam/           133-case adversarial corpus, metrics runner, injection eval
console/           static audit console over the ledger
tests/             178 tests across primitives, gates, store, executor, agent, API, REST adapter
scripts/demo.py    the six scenes
```

## Documentation

| File | What's in it |
|---|---|
| `BUILD_GUIDE.md` | Complete build guide: every component, every edge case, model selection |
| `ARCHITECTURE.md` | Trust boundaries, data model, gate-by-gate reference, executor state machine |
| `FAILURES.md` | What broke while building this and how it was fixed — including a real kernel bug |
| `RUNBOOK.md` | Operator runbook: config, incident playbooks, kill switch, going live |
| `docs/EVALUATION.md` | Generated metrics report with per-family breakdown |

## Standing on

- **AP2** (Agent Payments Protocol) — mandate shapes and the intent/cart split:
  [ap2-protocol.org](https://ap2-protocol.org/ap2/specification/),
  [Google Cloud announcement](https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol)
- **Razorpay Agentic Payments** — UPI Reserve Pay, In-App Commerce, MCP tools:
  [razorpay.com/agentic-payments](https://razorpay.com/agentic-payments/),
  [MCP server docs](https://razorpay.com/docs/mcp-server/)
- **NPCI Unified Agent Protocol** — the Indian rails this is designed to land on:
  [NDTV Profit explainer](https://www.ndtvprofit.com/personal-finance/letting-ai-pay-will-npcis-new-uap-framework-will-allow-bot-led-upi-spending-11749549)

## Licence and honesty notes

- The mock provider is a mock. The REST adapter targets Razorpay **test** mode and
  refuses non-test keys unless explicitly overridden.
- The user's signing key is derived from a seed for reproducible demos. In
  production it lives in a phone's secure element and never touches a server.
- `verify_chain()` proves internal consistency. It does not prove *when* an entry
  was written — external anchoring is listed as future work, not claimed as done.
