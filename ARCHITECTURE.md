# Architecture
## Thesis
Mandate Kernel separates proposal from authority.
The planner may read a goal and untrusted seller text, obtain a merchant quote,
and sign a `ProposedAction`.
It does not receive a payment-provider credential.
The deterministic Python kernel is the authority boundary.
It validates signed inputs, applies eight ordered gates, records every outcome,
and only then produces a short-lived, one-use capability for the executor.
The executor is the only layer that invokes the payment-provider interface to
create, capture, or refund a payment.
Payment amounts passed through mandates, quotes, actions, the kernel, and the
provider interface are integer Indian paise.
This is implemented by `Kernel.evaluate` in `kernel/pipeline.py` and
`Executor.execute` in `kernel/executor.py`.
## Trust boundaries
| Component | Trusts | Holds provider credentials? | Deterministic? |
|---|---|---:|---:|
| User signing device stand-in | User input and its private key | No | Yes in this demo |
| Planner | Goal, catalogue, and prompt playback as input data | No | Default planner: yes; optional LLM: no |
| Seller catalogue | Seller-authored product records | No | Yes |
| Seller quote service | Merchant private signing key | No | Yes |
| Agent proposal tool | The delegated agent private signing key | No | Yes |
| Policy kernel | Signed envelopes, key registry, SQLite state, configuration | No | Yes |
| Capability store | Kernel-minted capability records | No | Yes |
| Executor | An allowed verdict, capability, and provider adapter | Yes, when REST mode is configured | Yes |
| Razorpay adapter | Provider responses and configured credentials | Yes, in `RazorpayRestClient` | Network-dependent |
| SQLite ledger | Kernel mutations | No | Yes |
| Browser console | HTTP responses from the kernel | No | Yes |
The user, agent, merchant, and rogue-agent demo identities are all derived from
one `KEY_SEED` in `bootstrap.py:load_identities`.
That is a demo shortcut, not a production custody model.
The kernel only keeps public key records in `KeyRegistry` (`kernel/crypto.py`).
The REST adapter, not `kernel/`, reads `RAZORPAY_KEY_ID` and
`RAZORPAY_KEY_SECRET` (`adapters/razorpay_rest.py:RazorpayRestClient.__init__`).
The console is read-only JavaScript that queries `/healthz`, `/v1/ledger`, and
`/v1/trace/{action_id}` (`console/index.html`).
## Request path
```text
+-----------+     +-------------+     +------------------+     +----------------+
| User goal | --> | Planner     | --> | Seller catalogue | --> | Signed quote   |
| / approval|     | proposes    |     | and quote        |     | CartMandate    |
+-----------+     +-------------+     +------------------+     +----------------+
       |                 |                    |                         |
       | signs           | signs              | merchant signs          |
       v                 v                    v                         v
+----------------+  +------------------+  +------------------------------------+
| IntentMandate  |  | ProposedAction   |  | Kernel.evaluate                       |
| user-signed    |  | agent-signed     |  | 1 schema -> 2 signature -> 3 freshness|
+----------------+  +------------------+  | 4 budget -> 5 allowlist -> 6 price   |
                                           | 7 velocity -> 8 idempotency          |
                                           +----------------+-------------------+
                                                            | allow only
                                                            v
                                               +---------------------------+
                                               | Capability, 90 s default  |
                                               | opaque, exact scope, once |
                                               +-------------+-------------+
                                                             |
                                                             v
+----------------+  +----------------------+  +---------------------------+
| Audit console  | <-| hash-chained SQLite |<-| Executor redeems and burns |
| ledger / trace |  | ledger + state tables |  | capability before provider |
+----------------+  +----------------------+  +-------------+-------------+
                                                             |
                                                             v
                                                   +------------------+
                                                   | Razorpay adapter |
                                                   | mock, REST, aliases |
                                                   +------------------+
```
`RAZORPAY_MODE` accepts `mock` and `rest`; `razorpay`, `test`, and `live_test`
are aliases for the REST client.
A human approval interrupt is in front of the graph's `gate` node when the
agent graph is not in auto-approve mode (`agent/graph.py:build_graph`).
The graph compiles with `interrupt_before=["gate"]` in that mode.
The agent-side proposal call is not a money-moving call;
`agent/tools.py:propose_payment` returns the kernel verdict.
`agent/tools.py:execute_capability` is the only agent tool marked `money: True`.
The seller's MCP server exposes search, quote, and merchant information only;
it has no payment tool (`seller/mcp_server.py:TOOLS`).
## Signed data model
### Envelope
Every wire object is an `Envelope` with exactly these fields:
| Field | Type / role |
|---|---|
| `payload` | Dictionary that is signed |
| `sig` | Dictionary containing signature metadata |
The signing helper emits `sig.alg`, `sig.key_id`, and `sig.value`
(`kernel/crypto.py:sign_payload`).
The algorithm is exactly `Ed25519`.
The signature covers the payload only, not the wrapper.
`verify_envelope` verifies the canonical payload and checks the expected key role
(`kernel/crypto.py:verify_envelope`).
### `Constraints`
`IntentMandate.constraints` has these fields:
| Field | Meaning |
|---|---|
| `max_total_paise` | Maximum cumulative spend in paise |
| `max_per_txn_paise` | Maximum single transaction in paise |
| `max_transactions` | Transaction count ceiling |
| `rate_per_minute` | Per-mandate admitted-request ceiling |
| `allowed_merchants` | Merchant identifiers allowed by the user |
| `allowed_payees` | Payee identifiers allowed by the user |
| `allowed_skus` | Explicit product identifiers allowed |
| `allowed_categories` | Product categories allowed |
| `denied_skus` | Product identifiers denied even if otherwise allowed |
| `denied_payees` | Payees denied even if otherwise allowed |
`Constraints._coherent` rejects an empty merchant allowlist, empty payee
allowlist, and a mandate that scopes neither SKUs nor categories
(`kernel/models.py:Constraints._coherent`).
### `IntentMandate`
The user signs this object:
| Field |
|---|
| `type` |
| `version` |
| `mandate_id` |
| `subject` |
| `delegated_agents` |
| `human_present` |
| `prompt_playback` |
| `currency` |
| `constraints` |
| `issued_at` |
| `expires_at` |
| `nonce` |
`type` is the literal `IntentMandate`.
`version` is the literal integer `1`.
`currency` is the literal `INR`.
`subject` is compared with the subject bound to the user public key.
`delegated_agents` is the authoritative list of agent key IDs permitted to
propose actions against the mandate.
`prompt_playback` is recorded for audit but is not a policy input to the gates.
`expires_at` must be after `issued_at` and may be no more than 30 days later
(`kernel/models.py:IntentMandate._window`).
### `CartItem`
The merchant's cart contains one or more of these objects:
| Field |
|---|
| `sku` |
| `name` |
| `category` |
| `qty` |
| `unit_price_paise` |
| `tax_paise` |
A quantity is a strict positive integer.
`unit_price_paise` is strict positive integer paise.
`tax_paise` is non-negative integer paise.
### `CartMandate`
The merchant signs this price lock:
| Field |
|---|
| `type` |
| `version` |
| `cart_id` |
| `merchant_id` |
| `intent_ref` |
| `currency` |
| `items` |
| `subtotal_paise` |
| `tax_paise` |
| `shipping_paise` |
| `total_paise` |
| `payee` |
| `quoted_at` |
| `price_valid_until` |
| `nonce` |
`intent_ref` may be `null`, but when present Gate 2 requires it to equal the
current `IntentMandate.mandate_id` (`kernel/gates/g2_signature.py:gate`).
That is the cart-to-intent binding.
`CartMandate._non_empty` requires one to 200 unique SKU lines.
`CartMandate._window` requires `price_valid_until > quoted_at`
(`kernel/models.py`).
### `ProposedAction`
The delegated agent signs this request:
| Field |
|---|
| `type` |
| `version` |
| `action_id` |
| `action` |
| `amount_paise` |
| `currency` |
| `merchant_id` |
| `payee` |
| `intent_ref` |
| `cart_ref` |
| `cart_hash` |
| `attempt` |
| `attempt_class` |
| `client_nonce` |
| `rationale` |
| `reference_id` |
`action` is one of:
| Value |
|---|
| `create_order` |
| `create_payment_link` |
| `capture_payment` |
| `create_refund` |
`attempt_class` is one of:
| Value | Meaning |
|---|---|
| `initial` | First attempt for a cart |
| `retry` | Same instrument and idempotency key |
| `escalation` | Different instrument and a new idempotency key |
| `compensation` | Refund leg of a saga |
`capture_payment` and `create_refund` require `reference_id`.
`create_refund` must use `attempt_class=compensation`
(`kernel/models.py:ProposedAction._needs_reference`).
`intent_ref` must name the evaluated intent.
For cart-bearing actions, `cart_ref` must name the supplied cart.
`cart_hash` binds the proposal to `digest(cart payload)`.
Gate 6 computes that digest from the signed cart payload and compares it exactly
(`kernel/gates/g6_price.py:gate`).
### Binding summary
| Signed object | Signer role | Binding enforced |
|---|---|---|
| `IntentMandate` | User | Delegated agent IDs, user subject, budget and scope |
| `CartMandate` | Merchant | Merchant identity, quoted lines, totals, payee, optional `intent_ref` |
| `ProposedAction` | Agent | Intent reference, cart reference, cart hash, exact amount, payee and action |
Signature verification, using the key registry, checks both cryptographic
validity and role.
Gate 2 checks authority after validation.
A known agent key that is absent from `delegated_agents` is denied with
`G2_SIG_AGENT_NOT_DELEGATED`.
### Canonical JSON and signatures
`kernel/canonical.py:canonical_bytes` defines the signable representation.
The implementation accepts booleans, nulls, integers, strings, dictionaries,
lists, and tuples.
Tuples become JSON arrays.
It rejects floats.
It rejects non-string dictionary keys.
It rejects cycles.
It rejects nesting deeper than 64 levels.
It serializes with `json.dumps` using these settings:
```python
sort_keys=True
separators=(",", ":")
ensure_ascii=False
allow_nan=False
```
The resulting UTF-8 byte sequence is signed with Ed25519.
`digest` is SHA-256 of those canonical bytes (`kernel/canonical.py:digest`).
Floats are rejected because `4000.0` and `4000` are distinct source values but
can create ambiguous or implementation-dependent numeric representations.
The project therefore signs integer paise, not rupee decimals.
Strict Pydantic integer fields also reject a string amount or floating amount
before policy logic runs (`kernel/models.py`, `kernel/gates/g1_schema.py:gate`).
## Gate pipeline
`kernel/gates/__init__.py:PIPELINE` fixes this order:
```text
1 schema
2 signature
3 freshness
4 budget
5 allowlist
6 price_binding
7 velocity
8 idempotency
```
`Kernel.evaluate` stops at the first gate result whose decision is `deny`
(`kernel/pipeline.py:Kernel.evaluate`).
Only gates reached before the denial appear in `Verdict.gates`.
A gate exception is converted to a deny with `G1_SCHEMA_INVALID`.
### Schema
**Implementation:** `kernel/gates/g1_schema.py:gate`.
**Reads:** Raw envelope payloads and the declared action kind.
**Claims or mutates:** It parses and stores `ctx.action`, `ctx.intent`, and,
when present, `ctx.cart`; it makes no persistent state claim.
**Reason codes emitted by this implementation:**
| Reason code | Condition |
|---|---|
| `G1_SCHEMA_INVALID` | Invalid action, intent, or cart; bad intent/cart reference; incorrect cart presence |
| `G1_SCHEMA_UNKNOWN_FIELD` | An action payload has a forbidden extra field |
| `G1_SCHEMA_CURRENCY_UNSUPPORTED` | Action, intent, or cart is not INR |
The `Reason` enum members `SCHEMA_BAD_AMOUNT` and `SCHEMA_ACTION_UNSUPPORTED`
have wire values `G1_SCHEMA_BAD_AMOUNT` and `G1_SCHEMA_ACTION_UNSUPPORTED`,
but `g1_schema.py:gate` does not select either one.
Schema is first because every later gate depends on strict parsed types.
It also enforces that orders and payment links carry a cart, while capture and
refund actions do not.
### Signature
**Implementation:** `kernel/gates/g2_signature.py:gate`.
**Reads:** Parsed intent, action, cart, signed envelope dictionaries, and the
in-memory key registry.
**Claims or mutates:** It enriches `ctx.user_key`, `ctx.agent_key`, and
`ctx.merchant_key`; it makes no persistent state claim.
**Reason codes emitted:**
| Reason code | Condition |
|---|---|
| `G2_SIG_UNKNOWN_KEY` | Unknown or revoked signing key |
| `G2_SIG_BAD_ALG` | Algorithm is not `Ed25519` |
| `G2_SIG_INVALID` | Malformed, invalid, or role-confused signature |
| `G2_SIG_SUBJECT_MISMATCH` | User key subject differs from `IntentMandate.subject` |
| `G2_SIG_AGENT_NOT_DELEGATED` | Valid agent key is not named by the intent |
| `G2_SIG_MERCHANT_KEY_MISMATCH` | Merchant key subject differs from `CartMandate.merchant_id` |
| `G2_SIG_CART_NOT_BOUND_TO_INTENT` | Present cart `intent_ref` names another mandate |
Signature is second, rather than first, because the signed payload must first be
parsed into the strict schema needed to associate it with the proper object.
It follows schema so invalid untyped input cannot push signature code into
undefined assumptions.
It precedes freshness, money, and rate checks so unauthenticated content does
not consume work or mutate state beyond parsing.
### Freshness
**Implementation:** `kernel/gates/g3_freshness.py:gate`.
**Reads:** Parsed timestamp fields, the live `spend` state, current epoch
seconds, nonce cache, and `KernelConfig` clock-skew and nonce-TTL settings.
**Claims or mutates:** Calls `Store.nonce_seen`, an atomic check-and-set that
records the action nonce if it has not previously been seen in the computed
scope.
**Reason codes emitted:**
| Reason code | Condition |
|---|---|
| `G3_FRESH_INTENT_EXPIRED` | `expires_at < now` |
| `G3_FRESH_QUOTE_EXPIRED` | Cart `price_valid_until < now` |
| `G3_FRESH_ISSUED_IN_FUTURE` | Intent `issued_at` or cart `quoted_at` is beyond tolerated skew |
| `G3_FRESH_NONCE_REPLAY` | The scoped action client nonce was already recorded |
| `G3_FRESH_MANDATE_REVOKED` | The mandate is revoked and the action is not compensation |
The nonce scope is:
```text
{mandate_id}|{action}|{cart_ref}|{attempt}
```
The nonce is intentionally not the primary double-charge control.
The exact `(scope, client_nonce)` pair is one-use, so a retry must use a new
nonce or a different `attempt` value; Gate 8 supplies the stable charge key.
Freshness is third so stale or replayed authenticated content is rejected before
budget, rate, or idempotency state is touched.
### Budget
**Implementation:** `kernel/gates/g4_budget.py:gate`.
**Reads:** Intent constraints, action kind and amount, cart/mandate currency,
and `spend` state (`committed`, `reserved`).
**Claims or mutates:** For new spend it writes `ctx.scratch["reserve_paise"]`.
The pipeline performs `Store.reserve` only after all gates allow.
**Reason codes emitted:**
| Reason code | Condition |
|---|---|
| `G4_BUDGET_PER_TXN_EXCEEDED` | Amount exceeds `max_per_txn_paise` |
| `G4_BUDGET_TOTAL_EXCEEDED` | `committed + reserved + amount` exceeds `max_total_paise`, or arithmetic guard fails |
| `G4_BUDGET_CURRENCY_MISMATCH` | Cart and mandate currency differ |
| `G4_BUDGET_ZERO_AMOUNT` | Amount is not positive |
The zero-amount branch exists in Gate 4, although a normal wire request with an
`amount_paise` of zero is rejected earlier by `ProposedAction` validation.
Compensation is exempt from budget consumption.
Capture is checked against the per-transaction cap but does not add new
cumulative spend.
Budget is fourth because it needs an authenticated, fresh mandate and must run
before the pipeline reserves money.
### Allowlist
**Implementation:** `kernel/gates/g5_allowlist.py:gate`.
**Reads:** Intent merchant/payee/SKU/category allowlists and denylists, action
merchant and payee, and signed-cart merchant, payee, and line items.
**Claims or mutates:** No persistent claim.
**Reason codes emitted:**
| Reason code | Condition |
|---|---|
| `G5_ALLOW_MERCHANT_NOT_PERMITTED` | Merchant is outside the allowlist or action/cart merchant differs |
| `G5_ALLOW_PAYEE_NOT_PERMITTED` | Payee is missing from allowlist, suspicious, or action/cart payees differ |
| `G5_ALLOW_SKU_NOT_PERMITTED` | SKU is outside a SKU-only scope |
| `G5_ALLOW_CATEGORY_NOT_PERMITTED` | SKU category is outside the scope |
| `G5_ALLOW_DENYLIST_HIT` | Payee or SKU is denylisted |
`normalise_payee` strips configured zero-width characters, trims, NFKC
normalizes, and case-folds (`kernel/gates/g5_allowlist.py:normalise_payee`).
Any non-ASCII character after normalization is suspicious and is denied rather
than silently made equivalent to a trusted VPA.
Denylist checks precede allowlist acceptance.
With a cart, the cart's signed merchant identity is authoritative and must agree
with the action claim.
Allowlist is fifth because identity and scope are independent of the arithmetic
integrity established in the next gate.
### Price binding
**Implementation:** `kernel/gates/g6_price.py:gate`.
**Reads:** Signed cart lines and totals, action amount and cart hash, action
class, and `spend` state for cartless capture/refund actions.
**Claims or mutates:** Calculates `ctx.computed_cart_hash`; no persistent claim.
**Reason codes emitted:**
| Reason code | Condition |
|---|---|
| `G6_PRICE_LINE_MATH_MISMATCH` | Recomputed subtotal/tax differs or arithmetic guard fails |
| `G6_PRICE_CART_TOTAL_MISMATCH` | Recomputed total differs from cart total |
| `G6_PRICE_ACTION_AMOUNT_MISMATCH` | Order/link amount differs from cart total |
| `G6_PRICE_CART_HASH_MISMATCH` | Action hash differs from canonical signed cart payload digest |
| `G6_PRICE_QUANTITY_INVALID` | A cart item quantity is not positive |
| `G6_PRICE_REFUND_EXCEEDS_SETTLED` | Compensation amount exceeds settled spend |
| `G6_PRICE_NO_SETTLED_PAYMENT` | Compensation has no settled spend to refund |
| `G6_PRICE_CAPTURE_EXCEEDS_AUTHORISED` | Capture exceeds `committed + reserved` |
For cart actions, the exact equality is:
```text
sum(qty * unit_price_paise + tax_paise) + shipping_paise
  == total_paise
  == action.amount_paise
```
Price binding sits after scope validation and before velocity/idempotency because
it produces the canonical cart hash used by Gate 8's derived key.
### Velocity
**Implementation:** `kernel/gates/g7_velocity.py:gate`.
**Reads:** Config kill switch, persistent `flags.kill_switch`, `spend` state,
rate-event counts, mandate limits, and global rate configuration.
**Claims or mutates:** On allow, sets `ctx.scratch["record_rate"]`.
The pipeline records mandate and global rate events only after the full allow.
**Reason codes emitted:**
| Reason code | Condition |
|---|---|
| `G7_VEL_TXN_COUNT_EXCEEDED` | `txn_count` has reached `max_transactions` |
| `G7_VEL_RATE_LIMIT_EXCEEDED` | Per-mandate or global admitted request count has reached its 60-second limit |
| `G7_VEL_BREAKER_OPEN` | Per-mandate breaker is still in cooldown |
| `G7_VEL_KILL_SWITCH_ENGAGED` | Configured or database kill switch is on for non-compensation action |
A compensation `ProposedAction` that passes through the pipeline bypasses the
kill switch, breaker, and transaction-count checks, but still reaches rate
checks in this gate.  The direct `/v1/compensate` executor endpoint bypasses
the gates entirely, including those rate checks (`kernel/api.py:compensate`), so
`Executor.compensate` carries its own bound: the amount must be positive and no
greater than the mandate's committed plus reserved paise, or the call is refused
with `G6_PRICE_REFUND_EXCEEDS_SETTLED` / `G6_PRICE_NO_SETTLED_PAYMENT` and no
provider request is made.
Velocity is seventh so invalid or unauthenticated denial storms do not add
rate events.
### Idempotency
**Implementation:** `kernel/gates/g8_idempotency.py:gate`.
**Reads:** Mandate ID, action kind, computed cart hash, reference ID, amount,
attempt class, attempt number, and `idempotency` table state.
**Claims or mutates:** Derives `ctx.idempotency_key` and atomically claims a row
through `Store.idem_claim`.
**Reason codes emitted:**
| Reason code | Condition |
|---|---|
| `G8_IDEM_IN_FLIGHT` | Another worker owns a non-stale `in_flight` row |
| `G8_IDEM_REPLAYED_RESULT` | Existing terminal idempotency record is returned as `replayed_result` |
Derived key material is:
```text
order/link: {m: mandate_id, v: action, c: cart_hash, e: escalation_epoch}
capture/refund: {m: mandate_id, v: action, r: reference_id, a: amount_paise}
```
The key is `idem_` plus the first 32 hexadecimal characters of the canonical
SHA-256 digest (`kernel/gates/g8_idempotency.py:derive_key`).
A retry has epoch zero and reuses the key.
An escalation uses `attempt` as the epoch and receives a different key.
Idempotency is last because it acquires an in-flight claim.
Claiming it earlier would leave locks for requests that later fail a policy gate.
The pipeline releases an in-flight claim when a later failure needs unwinding,
although no gate follows Gate 8 in the normal order (`kernel/pipeline.py`).
`G8_IDEM_IN_FLIGHT` and `G8_IDEM_REPLAYED_RESULT` are non-punitive denials;
they do not advance the circuit breaker (`kernel/pipeline.py:_NON_PUNITIVE`).
## Capability tokens
`kernel/capability.py:mint` creates a token as `cap_` plus
`secrets.token_urlsafe(32)`.
The capability is server-side state, not a signed JWT.
The database record stores the full capability payload under the opaque token.
The capability model has these scope fields:
| Field |
|---|
| `token` |
| `action_id` |
| `mandate_id` |
| `idempotency_key` |
| `action` |
| `amount_paise` |
| `currency` |
| `merchant_id` |
| `payee` |
| `reference_id` |
| `issued_at` |
| `expires_at` |
The default lifetime is 90 seconds (`kernel/config.py:KernelConfig`).
`Store.capability_spend` reads the record and atomically changes `spent` from
zero to one before the provider request (`kernel/store.py:capability_spend`).
It rejects unknown, already-spent, and expired tokens.
`capability.redeem` then rechecks the exact amount, payee, and action verb
against the call being made (`kernel/capability.py:redeem`).
It does not recheck merchant ID or reference ID at redemption.
On the allow ledger event, the pipeline replaces the real bearer token with:
```text
cap_***<last six token characters>
```
The actual token is therefore not written to `verdict.allow` payloads
(`kernel/pipeline.py:Kernel.evaluate`).
This is redaction, not a token digest.
## Executor state machine
`ExecutionOutcome.state` may be one of the following terminal values:
| State | Meaning | Main path |
|---|---|---|
| `done` | Provider operation settled or reconciled | `_settle` |
| `failed` | Invalid verdict/capability, hard provider reject, or a refused, rejected, or transiently failed compensation | `execute` / `compensate` |
| `unknown` | Provider write is unresolved after reconciliation probes, or a refund timed out and may have landed | `_reconcile` / `compensate` |
| `stopped` | Attempt budget exhausted before or after transient failure | `execute` |
| `compensated` | Refund saga completed or its completed result replayed | `compensate` |
The executor refuses a verdict that is not an allow or has no capability.
Before provider contact, it reads `flags["attempts:{idempotency_key}"]`.
If prior attempts have reached `max_attempts_per_cart`, it releases the
reservation, marks idempotency failed, notes a denial, appends `exec.stopped`,
and returns `stopped` (`kernel/executor.py:Executor.execute`).
On transient provider failure it retries with the same key.
The sleep after attempt number `n` is:
```text
min(0.2 * (2 ** (n - 1)), 2.0) seconds
```
The default maximum attempts is three.
With that default, sleeps occur after first and second failures: 0.2 then 0.4
seconds (`kernel/executor.py:Executor.execute`).
A definitive `ProviderRejected` is not retried.
It releases the reservation, stores a failed idempotency result, appends
`exec.failed`, and returns `failed`.
Certain provider codes set `escalation_advised=True`; the executor does not
self-escalate.
The agent graph may make one escalation by building a payment-link proposal with
new attempt number and then sending it through all eight gates again
(`agent/graph.py:escalate`).
On `ProviderUnknownState`, the executor does not retry the write.
It calls `find_by_idempotency` up to three times.
Between misses it sleeps 0.2 then 0.4 seconds; there is deliberately no sleep
after the third probe, because no fourth probe follows it.
If a provider object is found, it appends `exec.reconciled` and settles it.
If none is found, it marks idempotency state `unknown`, leaves the reservation
held, sets persistent `flags.kill_switch` to `1`, appends `exec.unknown_state`,
and returns `unknown` with `requires_human=True`.
The reservation remains because the provider write may have succeeded.
After transient retries are exhausted, the executor releases the reservation,
marks the idempotency record failed, notes a denial, appends `exec.stopped`, and
returns `stopped`.
Compensation is direct executor logic, rather than redemption of the original
capability.
It derives `comp_{payment_id}_{amount_paise}`, claims it in the idempotency
table, calls provider refund, calls `Store.credit_refund`, appends
`exec.compensated`, and returns `compensated` (`kernel/executor.py:compensate`).
A compensation whose amount is not bounded by the mandate's committed plus
reserved paise appends `exec.compensation_refused` and returns `failed` before
any provider call.
A compensation rejection appends `exec.compensation_failed` and returns
`failed` requiring human intervention.
A transient or unknown-state refund failure appends
`exec.compensation_unresolved` and returns `failed` or `unknown` respectively,
both requiring human intervention.  The refund is never blind-retried and the
spend is not credited back, because neither outcome proves the money returned.
## Ledger and durable state
`kernel/store.py:SCHEMA` initializes SQLite in WAL mode with foreign keys and a
10-second busy timeout.
### `ledger`
| Column | Type / purpose |
|---|---|
| `seq` | Autoincrement integer primary key |
| `ts` | Epoch seconds |
| `kind` | Event type |
| `mandate_id` | Optional mandate linkage |
| `action_id` | Optional action linkage |
| `payload` | Canonical JSON text |
| `prev_hash` | Prior row hash or all-zero genesis hash |
| `hash` | This row hash |
There are indexes on `mandate_id` and `action_id`.
### `spend`
| Column | Type / purpose |
|---|---|
| `mandate_id` | Primary key |
| `committed_paise` | Settled spend |
| `reserved_paise` | Allowed but not settled spend |
| `txn_count` | Count incremented with reservation |
| `denial_streak` | Consecutive punitive denials |
| `breaker_until` | Breaker expiry epoch seconds |
| `revoked` | Integer boolean revocation flag |
### `nonces`
| Column | Type / purpose |
|---|---|
| `scope` | Composite primary-key component |
| `nonce` | Composite primary-key component |
| `seen_at` | Epoch seconds used for TTL deletion |
### `rate_events`
| Column | Type / purpose |
|---|---|
| `scope` | `mandate:{id}` or `global` |
| `ts` | Event epoch seconds |
There is an index on `(scope, ts)`.
### `idempotency`
| Column | Type / purpose |
|---|---|
| `key` | Derived primary key |
| `state` | `in_flight`, `succeeded`, or `failed` per schema comment; executor also writes `done` and `unknown` |
| `action_id` | Action that claimed it |
| `mandate_id` | Mandate that claimed it |
| `created_at` | Initial claim time |
| `updated_at` | Last state update time |
| `result` | Optional JSON result |
The schema comment and executor values are not identical.
`Store.idem_finish` persists whatever `state` string its caller supplies.
### `capabilities`
| Column | Type / purpose |
|---|---|
| `token` | Opaque capability primary key |
| `payload` | Canonical JSON capability payload |
| `spent` | Integer boolean burn marker |
| `expires_at` | Epoch seconds |
### `flags`
| Column | Type / purpose |
|---|---|
| `name` | Primary key |
| `value` | String value |
The database kill switch is stored as `flags["kill_switch"]` with value `1` or
`0`.
### Hash-chain construction
`Store.append` obtains the prior row hash or `GENESIS`, which is 64 zeroes.
It creates this canonical header object:
```text
{
  seq,
  ts,
  kind,
  mandate_id,
  action_id,
  payload_digest: digest(payload),
  prev_hash
}
```
It SHA-256 digests that header to produce `hash`.
It stores the canonical JSON payload text as well as the hash.
Any payload edit changes `payload_digest`.
Any header edit changes the recomputed row hash.
Any row deletion or altered predecessor link breaks the later `prev_hash` link.
`Store.verify_chain` reads rows in ascending sequence, parses payload JSON,
checks each `prev_hash`, reconstructs the header, and compares its digest with
the stored hash.
On success it returns `(True, None, "chain intact")`.
On failure it returns `(False, first_bad_seq, message)`.
It proves internal consistency of the ledger file from this genesis value.
It does **not** prove that the ledger was externally anchored.
It does **not** provide an independently witnessed timestamp.
It does **not** prevent a privileged attacker from replacing the whole database
with a newly self-consistent chain starting at the same all-zero genesis hash.
## Concurrency and atomic claims
`Store.transaction` begins outer transactions with `BEGIN IMMEDIATE`
(`kernel/store.py:Store.transaction`).
The store uses WAL mode, so readers can proceed while SQLite serializes writers.
`Kernel.evaluate` wraps the complete gate loop, reservation, rate recording,
capability minting, and verdict ledger append inside this transaction.
The protected invariant is that a budget check and its reservation cannot
interleave with another evaluator's check for the same database.
Gate 4 checks:
```text
committed_paise + reserved_paise + proposed amount
```
The pipeline increments `reserved_paise` only after all gates allow.
Because those operations share the transaction, two concurrent proposals cannot
both pass based on the same unreserved headroom
(`kernel/pipeline.py:Kernel.evaluate`, `kernel/store.py:Store.reserve`).
Nonce replay protection uses a delete-expired then insert pattern under the
unique primary key `(scope, nonce)`.
A `sqlite3.IntegrityError` means the nonce was already seen
(`kernel/store.py:Store.nonce_seen`).
Idempotency uses `Store.idem_claim` under the same transaction.
A missing row is inserted `in_flight`.
A non-stale in-flight row is returned to the caller rather than claimed again.
A row whose `updated_at` is more than 120 seconds old is reclaimed.
Capability burning performs a conditional update:
```sql
UPDATE capabilities SET spent = 1 WHERE token = ? AND spent = 0
```
The subsequent `changes()` check confirms exactly one winner
(`kernel/store.py:Store.capability_spend`).
## HTTP surface
`kernel/api.py` uses synchronous FastAPI handlers for the store-backed routes;
the raw-body webhook handler is the exception and is `async def`.
FastAPI executes the synchronous `def` handlers in a thread pool; this avoids
putting the blocking SQLite writer transaction inside an async event loop
(`kernel/api.py` module documentation).
| Method | Route | Purpose | Observed/implemented status codes |
|---|---|---|---|
| GET | `/healthz` | Liveness, provider name, ledger verification, kill state, DB path | 200 |
| GET | `/v1/keys` | Trusted demo public key IDs and algorithm | 200 |
| POST | `/v1/mandates/intent` | Demo user-device intent issuance and signing | 200; 422 validation |
| POST | `/v1/evaluate` | Run gates and, if allowed, retain pending action in memory | 200 allow; 403 deny; 422 request validation |
| POST | `/v1/execute` | Pop in-memory pending capability and execute | 200 done; 402 non-success; 409 unknown/consumed pending token; 422 validation |
| POST | `/v1/pay` | Evaluate then execute in one call | 200 done; 402 execution non-success; 403 policy deny; 409 replay; 422 validation |
| POST | `/v1/compensate` | Execute saga refund | 200 compensated; 502 any unresolved compensation — hard rejection, transient failure, or unknown state; 422 validation |
| GET | `/v1/mandates/{mandate_id}/state` | Read spend, revocation, breaker, and headroom | 200; 404 unknown mandate |
| POST | `/v1/mandates/{mandate_id}/revoke` | Mark mandate revoked and append event | 200; 422 malformed body |
| GET | `/v1/trace/{action_id}` | Ordered ledger trace for one action | 200; 404 absent action |
| GET | `/v1/ledger` | Most recent entries; `limit` clamped to the range 1–500, so neither a negative nor an oversized value can dump the whole ledger | 200 |
| GET | `/v1/ledger/verify` | Verify the hash chain | 200 |
| POST | `/v1/webhooks/razorpay` | Verify raw-body HMAC and log callback | 200 verified; 400 invalid signature |
| POST | `/v1/admin/kill-switch` | Persist kill-switch value and ledger event | 200; 422 validation |
| GET | `/openapi.json` | FastAPI generated OpenAPI schema | 200 |
| GET | `/docs` | FastAPI Swagger UI | 200 |
| GET | `/docs/oauth2-redirect` | Swagger OAuth redirect helper | 200 |
| GET | `/redoc` | FastAPI ReDoc UI | 200 |
The webhook handler verifies `x-razorpay-signature` over raw request bytes using
HMAC-SHA256 (`adapters/razorpay_rest.py:verify_webhook`).
It writes `webhook.rejected` even on bad signatures.
A verified webhook is logged as `webhook.accepted` but never changes budget;
it is only a reconciliation hint (`kernel/api.py:razorpay_webhook`).
## Deliberately not built
The repository intentionally contains a working demo implementation, not a
production payment deployment.
| Not built | What production would require |
|---|---|
| User private-key custody | Phone secure element, passkey, or HSM; the demo intent endpoint would not exist |
| Shared durable database | PostgreSQL or equivalent with operational backup, replication, and migrations |
| Externally witnessed ledger | Periodic hash anchoring to an independently controlled durable system |
| Trusted time proof | External time-stamping or a separately auditable time authority |
| Live UPI reserve-pay workflow | A real supported payment-rail integration and provider-specific operational controls |
| Tenant isolation | Tenant-scoped data, registry, policy state, credentials, and access control |
| Key lifecycle | Per-tenant key rotation, revocation history, and private-key custody separation |
| Distributed pending-capability cache | Redis or equivalent, because `_PENDING` is process-local |
| Webhook processing worker | Durable queueing and reconciliation workflow; current handler only logs hints |
| Provider result authentication | Provider-specific verified reconciliation and operational response controls |
The API intent endpoint is explicitly a demo stand-in for the user's signing
device (`kernel/api.py:create_intent`).
The real REST client defaults to refusing a non-`rzp_test_` key unless
`RAZORPAY_ALLOW_LIVE=1` is explicitly set
(`adapters/razorpay_rest.py:RazorpayRestClient.__init__`).
The repository does not implement a real UPI Reserve Pay integration.
