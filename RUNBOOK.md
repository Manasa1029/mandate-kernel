# Operator runbook
## Operating assumptions
Run commands from the repository root:
```bash
cd /home/user/workspace/mandate-kernel
```
The kernel service is a single-process Python/FastAPI application using SQLite.
Amounts are integer paise.
The default provider is the offline mock.
The default database is `kernel.db` (`kernel/config.py:KernelConfig.from_env`).
`python-dotenv` is listed as a dependency, but no reviewed startup path calls
`load_dotenv`.
Export variables in the shell that starts the process, or provide them through
Docker Compose.
## Quickstart
### Install dependencies
```bash
make install-dev
```
This runs `python -m pip install -r requirements-dev.txt` (`Makefile`).
Expected output begins with the pip install command and reports installed or
already-satisfied runtime and test dependencies.
### Verify the repository
```bash
make eval
```
`make eval` runs `make test`, `python -m redteam.runner`, and
`python -m redteam.injection` in that order (`Makefile`).
Expected output shape:
```text
178 passed, ... warning in ...s
cases                133
attack block rate    100.00%  (74/74)
false positive rate  0.00%  (0/59)
reason accuracy      100.00%
latency p50/p95      ...us / ...us
ledger intact        True
hostile listings      4
kernel containment    100%
```
The red-team runner rewrites `docs/EVALUATION.md` and
`docs/redteam_results.json` (`redteam/runner.py:write_report`).
The injection runner rewrites `docs/injection_results.json`
(`redteam/injection.py:main`).
The exact latency values are host- and run-dependent.
### Run the terminal demo
```bash
make demo DB=demo.db
```
`make demo` executes all six scenes and uses the supplied SQLite path
(`Makefile`, `scripts/demo.py:main`).
Expected output includes `SCENE 1` through `SCENE 6`, then `INTEGRITY`.
A successful final line says the hash chain verified over one or more ledgers.
The demo's default mock provider means this command does not call Razorpay.
### Start the kernel API
```bash
make api DB=kernel.db PORT=8000
```
The Make target starts:
```bash
python -m uvicorn kernel.api:app --reload --port 8000
```
It runs in the foreground.
Expected startup output identifies Uvicorn and listens on `127.0.0.1:8000`.
In a separate shell, check it with:
```bash
curl -sS http://127.0.0.1:8000/healthz | python -m json.tool
```
Expected fields include `ok`, `provider`, `ledger_intact`, `kill_switch`, and
`db` (`kernel/api.py:healthz`).
### Start the audit console
```bash
make console
```
This runs Python's static file server on port 8080 with `console` as its
directory (`Makefile`).
The console defaults to API base URL `http://127.0.0.1:8000`
(`console/index.html`).
It displays ledger-chain state, provider name, kill-switch state, ledger rows,
and action traces.
## Configuration reference
`KernelConfig.from_env` reads the kernel variables below
(`kernel/config.py:KernelConfig.from_env`).
The seller and planner read their own environment variables directly.
| Variable | Default | Effect and risk of raising/lowering |
|---|---|---|
| `KERNEL_DB_PATH` | `kernel.db` | SQLite ledger path. `:memory:` loses the ledger on process exit; a wrong path can split audit history. |
| `RAZORPAY_MODE` | `mock` | Provider factory mode. `mock` is offline. `rest` selects the real Razorpay REST client; `razorpay`, `test`, and `live_test` are accepted aliases for it. Anything else raises `ValueError` at startup. |
| `RAZORPAY_KEY_ID` | unset; REST startup raises `KeyError` | Required by the REST adapter. A test key must start `rzp_test_`; a live-looking key is refused unless the separate override is set. |
| `RAZORPAY_KEY_SECRET` | unset; REST startup raises `KeyError` | Required for authenticated REST calls. Exposing it permits provider API calls within its Razorpay permissions. |
| `RAZORPAY_ALLOW_LIVE` | unset, therefore refusal | Only checked by `RazorpayRestClient` for a non-test key. Setting `1` permits live keys and can move real money. |
| `RAZORPAY_WEBHOOK_SECRET` | empty | HMAC secret for callbacks. Empty makes every webhook verification fail; use a provider-side secret to accept callbacks. Exposing the configured secret would permit forged callbacks to appear verified. |
| `KERNEL_CLOCK_SKEW_S` | `30` | Future-timestamp tolerance. Raising widens acceptance of future-issued intent/cart timestamps; lowering increases clock-drift denials. |
| `KERNEL_NONCE_TTL_S` | `86400` | Replay-cache retention. Lowering below an intended mandate lifetime can forget a replay; raising grows the nonce table. |
| `KERNEL_BREAKER_THRESHOLD` | `5` | Consecutive punitive denials before a mandate breaker opens. Raising tolerates more agent mistakes; lowering freezes mandates sooner. |
| `KERNEL_BREAKER_COOLDOWN_S` | `300` | Breaker duration. Raising extends protective downtime; lowering permits a looping agent to resume sooner. |
| `KERNEL_GLOBAL_RPM` | `120` | Global admitted-request limit per 60 seconds. Raising reduces provider protection; lowering can block unrelated mandates. |
| `KERNEL_CAPABILITY_TTL_S` | `90` | Capability lifetime. Raising expands bearer-token exposure; lowering increases expiry failures under latency. |
| `KERNEL_MAX_ATTEMPTS` | `3` | Provider attempts per idempotency key. Raising increases repeated provider contact; lowering stops recoverable transients earlier. |
| `KERNEL_PROVIDER_TIMEOUT_S` | `8.0` | Read timeout in seconds for REST provider calls, threaded through `build_provider` into the adapter's HTTP client. The connect timeout is fixed at 3.0s. |
| `KERNEL_KILL_SWITCH` | false | Startup-only config kill switch. Setting true blocks new non-compensation proposals, including after database switch-off, until restart with false. |
| `MODEL_PROVIDER` | empty in code; `.env.example` says `deterministic` | `build_planner` enables LLM only for `anthropic` or `openai` with its matching key. Any other/no provider selects `DeterministicPlanner`. |
| `ANTHROPIC_API_KEY` | empty | Enables Anthropic planner only with `MODEL_PROVIDER=anthropic`. It does not affect kernel decisions. |
| `OPENAI_API_KEY` | empty | Enables OpenAI planner only with `MODEL_PROVIDER=openai`. It does not affect kernel decisions. |
| `MODEL_NAME` | `claude-sonnet-4-5` for Anthropic; otherwise `gpt-4.1` | Optional model name passed to the optional planner. Planner-construction failures fall back to deterministic planning; model-request failures occur later and are not caught here. |
| `MODEL_TEMPERATURE` | documented as `0`; not read | `.env.example` declares it, but reviewed code does not read this environment variable. `LLMPlanner` defaults its constructor temperature to `0.0`. |
| `KEY_SEED` | `mandate-kernel-demo-seed-do-not-use-in-prod` | Derives all demo keys. Changing it changes every key ID and invalidates interoperability with objects signed under the old seed. Never use the default in a real deployment. |
The `.env.example` also documents no seller variables, but `seller/app.py` reads:
| Variable | Default | Effect and risk |
|---|---|---|
| `QUOTE_TTL_S` | `300` | Merchant price-lock life. Raising extends stale-price exposure; lowering causes more re-quotes. |
| `SHIPPING_PAISE` | `4000` | Seller quote shipping amount in paise. Changing it changes signed quote totals. |
| `FREE_SHIP_ABOVE_PAISE` | `50000` | Subtotal threshold for zero shipping. Lowering gives free shipping more readily; raising applies shipping more often. |
| `FULFIL_FAIL_SKUS` | `SKU-GHEE-BULK` | Comma-separated demo fulfilment failures. It controls the saga demonstration, not kernel authorization. |
| `DEMO_USER` | `user_nikitha` | Demo user subject; changes derived identity. |
| `DEMO_AGENT` | `agent_pantry_bot` | Demo delegated agent subject; changes derived identity. |
| `DEMO_MERCHANT` | `acme_pantry` | Demo merchant ID; changes derived identity and default allowlist compatibility. |
| `DEMO_PAYEE` | `acmepantry@hdfcbank` | Demo default VPA; changing it must agree with signed mandate allowlists. |
| `PORT` | `8000` | Used only by direct `python -m kernel.api`; the Make target uses its own `PORT` make variable. |
`KEY_SEED`, `DEMO_USER`, `DEMO_AGENT`, `DEMO_MERCHANT`, and `DEMO_PAYEE` are read
in `bootstrap.py` during import.
Set them before starting any process that imports `bootstrap`.
## Verification
### File-level chain verification
For the default file:
```bash
make verify
```
For a named ledger:
```bash
make verify DB=kernel.db
```
The target opens the database through `Store.verify_chain` and prints one of:
```text
INTACT — chain intact
BROKEN at seq <number> — <message>
```
It exits non-zero on a broken chain (`Makefile:verify`).
### API-level verification
```bash
curl -sS http://127.0.0.1:8000/v1/ledger/verify | python -m json.tool
```
An intact response is shaped as:
```json
{
    "intact": true,
    "first_bad_seq": null,
    "message": "chain intact"
}
```
The endpoint always returns HTTP 200; inspect `intact` rather than relying on
HTTP status (`kernel/api.py:ledger_verify`).
### If verification reports `BROKEN at seq N`
1. Stop new writes before doing forensic work.
2. If the API is running, engage the kill switch only if no unknown provider state is already being investigated:
   ```bash
   curl -sS -X POST http://127.0.0.1:8000/v1/admin/kill-switch \
     -H 'content-type: application/json' \
     -d '{"on":true,"reason":"ledger verification investigation"}' | python -m json.tool
   ```
3. Stop the API process after recording its current response; the kill-switch call itself appends a ledger row.
4. Copy the database **and** its `-wal` and `-shm` sidecars before opening it with diagnostic tools:
   ```bash
   mkdir -p incident-ledger-copy
   cp kernel.db kernel.db-wal kernel.db-shm incident-ledger-copy/ 2>/dev/null || true
   ```
5. Re-run verification against the preserved copy:
   ```bash
   make verify DB=incident-ledger-copy/kernel.db
   ```
6. Do not delete the bad row or regenerate hashes in the original database.
7. Investigate the first bad sequence and restore a known-good database copy only under an approved incident process.
8. Keep external provider reconciliation records with the preserved ledger copy.
`verify_chain` detects invalid payload JSON, mismatched predecessor hash, and
header/payload hash mismatch (`kernel/store.py:Store.verify_chain`).
It cannot establish an externally witnessed timestamp or prove that a complete,
self-consistent database replacement never occurred.
## Incident playbooks
### Provider returned unknown state; mandate frozen
**Symptom:** execution returns `state: "unknown"` and
`reason: "X_EXEC_UNKNOWN_STATE_RESOLVED"`.
**Diagnosis:**
```bash
curl -sS http://127.0.0.1:8000/healthz | python -m json.tool
curl -sS http://127.0.0.1:8000/v1/mandates/MANDATE_ID/state | python -m json.tool
curl -sS 'http://127.0.0.1:8000/v1/ledger?limit=500' | python -m json.tool
```
Look for `exec.unknown_state`, a held `reserved_paise`, and `kill_switch: true`.
**Resolution:**
1. Do not resend the payment.
2. Reconcile at the provider using the idempotency key shown in the
   `exec.unknown_state` payload.
3. If the provider confirms the write landed, preserve the provider result and
   settle through a controlled recovery script reviewed by an operator.
4. If the provider proves no write landed, release only the known reservation,
   finalize the idempotency record, and append an audit event in one transaction:
```bash
MANDATE_ID='mnd_example' AMOUNT_PAISE=12345 IDEM_KEY='idem_example' python - <<'PY'
import os
from kernel.store import Store
s = Store(os.environ.get('KERNEL_DB_PATH', 'kernel.db'))
with s.transaction():
    s.release_reservation(os.environ['MANDATE_ID'], int(os.environ['AMOUNT_PAISE']))
    s.idem_finish(os.environ['IDEM_KEY'], 'failed', {'reason': 'provider_confirmed_not_landed'})
    s.append('exec.failed', {'idempotency_key': os.environ['IDEM_KEY'],
                             'reconciled_by': 'operator',
                             'provider_confirmed': 'not_landed'},
             mandate_id=os.environ['MANDATE_ID'])
PY
```
5. Confirm the reservation is zero and the ledger remains intact:
```bash
make verify DB=kernel.db
curl -sS http://127.0.0.1:8000/v1/mandates/MANDATE_ID/state | python -m json.tool
```
6. Disengage the persistent switch only after reconciliation of every unresolved
provider write; see the kill-switch procedure.
`Executor._reconcile` deliberately holds the reservation and persists the kill
switch after three unsuccessful probes (`kernel/executor.py:_reconcile`).
There is no dedicated HTTP endpoint for settling or releasing an unknown state.
### Circuit breaker open on a mandate
**Symptom:** a proposal is denied with `G7_VEL_BREAKER_OPEN`.
**Diagnosis:**
```bash
curl -sS http://127.0.0.1:8000/v1/mandates/MANDATE_ID/state | python -m json.tool
curl -sS 'http://127.0.0.1:8000/v1/ledger?limit=500' | python -m json.tool
```
Inspect `breaker_until` and preceding `verdict.deny` or `exec.stopped` events.
**Resolution:**
1. Stop the agent run that generated consecutive denials.
2. Correct its input, quote, scope, or provider configuration.
3. Wait until `breaker_until` is no longer in the future.
4. If the user no longer wants the authority, revoke the mandate:
```bash
curl -sS -X POST http://127.0.0.1:8000/v1/mandates/MANDATE_ID/revoke \
  -H 'content-type: application/json' \
  -d '{"reason":"breaker investigation"}' | python -m json.tool
```
5. Confirm a new proposal no longer returns breaker-open, or confirm the mandate
is revoked.
There is no supported API for manually clearing `breaker_until`.
`Store.note_denial` opens the breaker when its threshold is reached and resets
the denial streak (`kernel/store.py:Store.note_denial`).
### Reservation stuck or headroom leaked
**Symptom:** `reserved_paise` remains non-zero after a known failed execution.
**Diagnosis:**
```bash
curl -sS http://127.0.0.1:8000/v1/mandates/MANDATE_ID/state | python -m json.tool
curl -sS http://127.0.0.1:8000/v1/trace/ACTION_ID | python -m json.tool
```
A hard reject and exhausted transient retry should call `release_reservation`.
An unresolved unknown state intentionally should not.
**Resolution:**
1. First determine whether the provider write may have landed.
2. Follow the unknown-state procedure if it may have landed.
3. For a confirmed pre-settlement failure, use the controlled release snippet in
   the unknown-state playbook with the exact reservation amount.
4. Do not use `release_reservation` for a settled provider payment.
5. Confirm `reserved_paise` decreased by the exact amount, `headroom_paise`
   increased accordingly, and `make verify DB=kernel.db` remains intact.
`Store.release_reservation` also decrements `txn_count` by default
(`kernel/store.py:Store.release_reservation`).
### Suspected forged webhook traffic
**Symptom:** POST `/v1/webhooks/razorpay` returns 400 or ledger records
`webhook.rejected`.
**Diagnosis:**
```bash
curl -sS 'http://127.0.0.1:8000/v1/ledger?limit=500' | python -m json.tool
curl -sS http://127.0.0.1:8000/healthz | python -m json.tool
```
The logged payload includes event name, `verified`, signature presence, raw byte
length, and selected entity IDs (`kernel/api.py:razorpay_webhook`).
**Resolution:**
1. Preserve relevant ledger and web-server logs.
2. Confirm the configured `RAZORPAY_WEBHOOK_SECRET` matches the provider-side
   secret.
3. Rotate that secret with the provider if compromise is suspected.
4. Restart the API process with the new environment variable.
5. Send a known correctly signed test webhook from the provider tooling.
6. Confirm HTTP 200 and a `webhook.accepted` entry.
Do not treat even a verified webhook as a budget update.
The handler logs it as a reconciliation hint and never calls a spend mutation.
### Capability token leaked
**Symptom:** a `cap_...` bearer token was exposed before its expiry.
**Diagnosis:**
1. Locate the action and mandate using the issuing client or in-memory process
   context; full tokens are redacted from `verdict.allow` ledger payloads.
2. Inspect the mandate state and relevant trace:
```bash
curl -sS http://127.0.0.1:8000/v1/mandates/MANDATE_ID/state | python -m json.tool
curl -sS http://127.0.0.1:8000/v1/trace/ACTION_ID | python -m json.tool
```
**Resolution:**
1. Engage the kill switch to stop new non-compensation evaluations.
2. Treat a capability already redeemed as a potential provider-state incident.
3. If the capability is confirmed unredeemed, burn it and release its still
   in-flight pre-execution reservation in a single controlled script:
```bash
CAP_TOKEN='cap_example' python - <<'PY'
import os
from kernel.store import Store
s = Store(os.environ.get('KERNEL_DB_PATH', 'kernel.db'))
with s.transaction():
    ok, cap, why = s.capability_spend(os.environ['CAP_TOKEN'])
    if not ok:
        raise SystemExit(f'not burned: {why}; reconcile before any release')
    s.release_reservation(cap['mandate_id'], cap['amount_paise'])
    s.idem_release(cap['idempotency_key'])
    s.append('exec.capability_rejected',
             {'reason': 'X_EXEC_CAPABILITY_SPENT', 'detail': 'operator invalidated leaked capability'},
             mandate_id=cap['mandate_id'], action_id=cap['action_id'])
PY
```
4. Confirm the token cannot be spent, reservation is released, and chain verifies.
5. Restart the API process if the capability may still be present in `_PENDING`.
The database burn is atomic, but `_PENDING` is an in-process cache in
`kernel/api.py`; a restart clears it.
### Agent loop hitting velocity limits
**Symptom:** `G7_VEL_RATE_LIMIT_EXCEEDED` or
`G7_VEL_TXN_COUNT_EXCEEDED` denials appear.
**Diagnosis:**
```bash
curl -sS http://127.0.0.1:8000/v1/mandates/MANDATE_ID/state | python -m json.tool
curl -sS 'http://127.0.0.1:8000/v1/ledger?limit=500' | python -m json.tool
```
**Resolution:**
1. Stop the caller or agent process; the kernel has no route to stop an external
   planner loop.
2. Preserve the ledger trace and identify the repeated proposal pattern.
3. Wait at least 60 seconds for rate-event windows to age out.
4. For exhausted `max_transactions`, obtain a new user-signed mandate; the
   existing limit is signed policy and cannot be raised by configuration.
5. Revoke the mandate if the user wants all new non-compensation activity
   stopped.
6. Confirm rate or transaction denials no longer occur before resuming.
Rate events are recorded only for admitted requests (`kernel/gates/g7_velocity.py:gate`).
### Ledger verification failure
**Symptom:** `make verify` exits non-zero or API reports `"intact": false`.
**Diagnosis:**
```bash
make verify DB=kernel.db
curl -sS http://127.0.0.1:8000/v1/ledger/verify | python -m json.tool
```
**Resolution:** follow the full verification-failure procedure above.
Do not run `make fresh`; it deletes the ledger and its WAL/SHM files
(`Makefile:fresh`).
**Recovery confirmation:** `make verify DB=kernel.db` prints `INTACT — chain intact`.
### Live Razorpay keys configured by accident
**Symptom:** `RAZORPAY_KEY_ID` does not start with `rzp_test_`, and
`RAZORPAY_ALLOW_LIVE=1` allows startup.
**Diagnosis:**
```bash
curl -sS http://127.0.0.1:8000/healthz | python -m json.tool
```
Also inspect the process environment using the deployment platform's secure
operational controls; do not paste secrets into shell history or logs.
**Resolution:**
1. Engage the kill switch.
2. Stop the API process immediately because a previously minted capability in
   `_PENDING` can still be executed by `Executor.execute`, which does not check
   the kill switch.
3. Revoke/rotate the live provider credentials in Razorpay.
4. Replace them with `rzp_test_` credentials or remove them.
5. Ensure `RAZORPAY_ALLOW_LIVE` is unset or not `1`.
6. Restart in mock or test mode.
7. Reconcile any provider activity during the exposure window.
8. Confirm startup refuses a live-looking key without the override; the adapter
   raises `RuntimeError` in that case.
The REST adapter emits a critical warning only after the explicit live override
(`adapters/razorpay_rest.py:RazorpayRestClient.__init__`).
## Kill switch
### Engage
```bash
curl -sS -X POST http://127.0.0.1:8000/v1/admin/kill-switch \
  -H 'content-type: application/json' \
  -d '{"on":true,"reason":"operator incident response"}' | python -m json.tool
```
### Disengage
```bash
curl -sS -X POST http://127.0.0.1:8000/v1/admin/kill-switch \
  -H 'content-type: application/json' \
  -d '{"on":false,"reason":"operator recovery complete"}' | python -m json.tool
```
### Behavior while engaged
Gate 7 denies new non-compensation proposals with
`G7_VEL_KILL_SWITCH_ENGAGED`.
A compensation proposal is exempt at Gate 7.
`Executor.compensate` does not consult the kill switch and can refund a captured
payment while it is on (`kernel/executor.py:Executor.compensate`).
Existing capabilities are not invalidated by this flag.
The executor has no kill-switch check at redemption.
Stop the API process or burn known unredeemed capabilities if immediate blocking
of existing `_PENDING` work is necessary.
`KERNEL_KILL_SWITCH=true` is a separate configuration-level switch that remains
active after database switch-off until a restart with false.
### Confirm
```bash
curl -sS http://127.0.0.1:8000/healthz | python -m json.tool
curl -sS 'http://127.0.0.1:8000/v1/ledger?limit=10' | python -m json.tool
```
Check `kill_switch: true` and a recent `admin.kill_switch` ledger event.
## Switching from mock to Razorpay test mode
The provider factory accepts the `.env.example` value `rest`.
It also accepts `test`, `razorpay`, and `live_test` as aliases
(`adapters/__init__.py:build_provider`).
### Pre-flight checklist
- A fresh, backed-up SQLite ledger path is selected.
- `make verify DB=kernel.db` is intact before the switch.
- `RAZORPAY_KEY_ID` begins exactly with `rzp_test_`.
- `RAZORPAY_KEY_SECRET` is supplied through a secret mechanism.
- `RAZORPAY_ALLOW_LIVE` is unset or not `1`.
- A webhook secret is configured if callbacks will be sent.
- The kill switch is off only after the above checks.
- Test payment and refund procedures are ready.
### Steps
1. Stop the mock API.
2. Export test-mode settings in the shell that will start Uvicorn:
```bash
export RAZORPAY_MODE=rest
export RAZORPAY_KEY_ID='rzp_test_replace_me'
export RAZORPAY_KEY_SECRET='replace_me'
export RAZORPAY_WEBHOOK_SECRET='replace_me'
unset RAZORPAY_ALLOW_LIVE
```
3. Start the API:
```bash
make api DB=kernel.db PORT=8000
```
4. Check its health response:
```bash
curl -sS http://127.0.0.1:8000/healthz | python -m json.tool
```
5. Confirm `provider` is `razorpay-test`.
6. Issue a test intent and execute only a controlled test proposal.
7. Verify ledger entries and reconcile the resulting provider object by its
   stored idempotency key.
For orders the adapter sends the idempotency key as Razorpay `receipt`.
For payment links it uses the first 40 characters as `reference_id`.
The adapter also records `notes.idem_key` on create operations
(`adapters/razorpay_rest.py`).
## Routine operations
### Rotate `KEY_SEED`
`KEY_SEED` changes all deterministically derived demo identity key IDs.
It is not a safe in-place production key-rotation mechanism.
1. Stop all API, seller, and agent processes.
2. Back up and verify the existing ledger.
3. Record old key IDs from `GET /v1/keys`.
4. Start all cooperating demo components with the same new seed.
5. Do not submit old signed mandates or carts to the new registry; their key IDs
   are no longer recognized.
6. Issue fresh mandates and quotes.
Production needs per-tenant key histories and rotation, not a shared seed
(`kernel/crypto.py:KeyRegistry` is in-memory only).
### Revoke a mandate
```bash
curl -sS -X POST http://127.0.0.1:8000/v1/mandates/MANDATE_ID/revoke \
  -H 'content-type: application/json' \
  -d '{"reason":"user_requested"}' | python -m json.tool
```
This sets `spend.revoked=1` and appends `mandate.revoked`
(`kernel/api.py:revoke`, `kernel/store.py:Store.revoke_mandate`).
New non-compensation actions are denied in Gate 3.
Compensation remains permitted by the freshness gate.
### Read an action trace
```bash
curl -sS http://127.0.0.1:8000/v1/trace/ACTION_ID | python -m json.tool
```
The response includes ordered entries with `seq`, `ts`, `kind`, `payload`, and
`hash` (`kernel/store.py:Store.trace`).
Use `/v1/ledger?limit=500` to find recent action IDs.
### Back up the ledger
Use SQLite's online backup command, then verify the copy:
```bash
sqlite3 kernel.db ".backup 'kernel-backup.db'"
make verify DB=kernel-backup.db
```
If the database is not cleanly checkpointed, preserve its `-wal` and `-shm`
sidecars with the primary file for forensic backup.
Do not use `cp` alone as the normal online backup method.
## Known limitations
- SQLite permits one writer at a time; `BEGIN IMMEDIATE` serializes writers.
- The API `_PENDING` map is process-local, not durable, and is lost on restart.
- A capability record is durable in SQLite, but `/v1/execute` requires the
  matching in-memory pending verdict/action entry.
- `verify_chain` proves only internal chain consistency; there is no external
  anchoring or trusted timestamping.
- The key registry is in-memory and has no persistent rotation history.
- The demo keys are deterministically derived from one seed.
- The user-signing endpoint is a demo stand-in and should not exist in
  production.
- The project has no built-in operator endpoint for clearing a circuit breaker,
  resolving an unknown provider state, or releasing a manually confirmed stuck
  reservation.
- Webhooks are logged hints, not direct financial state transitions.
- Direct `/v1/compensate` calls bypass the gate pipeline, including Gate 7 rate
  checks. `Executor.compensate` enforces its own amount bound instead: the refund
  must be positive and no larger than the mandate's committed plus reserved paise,
  otherwise it is refused with `G6_PRICE_REFUND_EXCEEDS_SETTLED` or
  `G6_PRICE_NO_SETTLED_PAYMENT` before any provider call. Still protect the
  endpoint at the deployment boundary, since it is not rate limited.
- The real adapter has no first-class universal Razorpay idempotency header; it
  reconciles create operations by receipt/reference fields.
- The test suite and red-team evaluation do not exercise a live payment rail.
