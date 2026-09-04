"""Durable state: hash-chained audit ledger, budget ledger, nonce cache,
velocity counters, breaker state, idempotency records, capability records.

Why one module: these tables must mutate together inside a single transaction.
A budget check that is not in the same transaction as the reservation is a
time-of-check/time-of-use bug, and TOCTOU on money is the whole point of this
project. `Store.transaction()` issues `BEGIN IMMEDIATE`, so two concurrent
requests against the same mandate serialise instead of both passing Gate 4.

SQLite in WAL mode is enough for a buildathon and swaps for Postgres by
replacing this file only — no gate imports sqlite3.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .canonical import canonical_bytes, digest
from .models import now_s

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  kind       TEXT    NOT NULL,
  mandate_id TEXT,
  action_id  TEXT,
  payload    TEXT    NOT NULL,
  prev_hash  TEXT    NOT NULL,
  hash       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_mandate ON ledger(mandate_id);
CREATE INDEX IF NOT EXISTS ledger_action  ON ledger(action_id);

CREATE TABLE IF NOT EXISTS spend (
  mandate_id      TEXT PRIMARY KEY,
  committed_paise INTEGER NOT NULL DEFAULT 0,
  reserved_paise  INTEGER NOT NULL DEFAULT 0,
  txn_count       INTEGER NOT NULL DEFAULT 0,
  denial_streak   INTEGER NOT NULL DEFAULT 0,
  breaker_until   INTEGER NOT NULL DEFAULT 0,
  revoked         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS nonces (
  scope   TEXT NOT NULL,
  nonce   TEXT NOT NULL,
  seen_at INTEGER NOT NULL,
  PRIMARY KEY (scope, nonce)
);

CREATE TABLE IF NOT EXISTS rate_events (
  scope TEXT    NOT NULL,
  ts    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_scope_ts ON rate_events(scope, ts);

CREATE TABLE IF NOT EXISTS idempotency (
  key        TEXT PRIMARY KEY,
  state      TEXT NOT NULL,          -- in_flight | succeeded | failed
  action_id  TEXT NOT NULL,
  mandate_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  result     TEXT
);

CREATE TABLE IF NOT EXISTS capabilities (
  token      TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  spent      INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS flags (
  name  TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0

    # ---------------------------------------------------------------- plumbing

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Reentrant BEGIN IMMEDIATE. Serialises writers; readers still proceed (WAL)."""
        with self._lock:
            top = self._depth == 0
            if top:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except Exception:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ ledger

    def append(self, kind: str, payload: dict[str, Any], *, mandate_id: str | None = None,
               action_id: str | None = None, ts: int | None = None) -> tuple[int, str]:
        """Append one tamper-evident row. Returns (seq, hash).

        The row hash covers the previous hash, so any edit to history invalidates
        every subsequent row. We store the payload verbatim *and* hash its
        canonical digest, so re-serialisation differences cannot break the chain.
        """
        with self.transaction() as c:
            row = c.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = row["hash"] if row else GENESIS
            next_seq = (row["seq"] + 1) if row else 1
            stamp = ts if ts is not None else now_s()
            head = {
                "seq": next_seq,
                "ts": stamp,
                "kind": kind,
                "mandate_id": mandate_id,
                "action_id": action_id,
                "payload_digest": digest(payload),
                "prev_hash": prev_hash,
            }
            h = digest(head)
            c.execute(
                "INSERT INTO ledger (seq, ts, kind, mandate_id, action_id, payload, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (next_seq, stamp, kind, mandate_id, action_id,
                 canonical_bytes(payload).decode(), prev_hash, h),
            )
            return next_seq, h

    def verify_chain(self) -> tuple[bool, int | None, str]:
        """Recompute every row hash. Returns (ok, first_bad_seq, message)."""
        prev = GENESIS
        for row in self._conn.execute("SELECT * FROM ledger ORDER BY seq ASC"):
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                return False, row["seq"], "payload is not valid JSON"
            if row["prev_hash"] != prev:
                return False, row["seq"], "prev_hash does not match previous row hash"
            head = {
                "seq": row["seq"],
                "ts": row["ts"],
                "kind": row["kind"],
                "mandate_id": row["mandate_id"],
                "action_id": row["action_id"],
                "payload_digest": digest(payload),
                "prev_hash": row["prev_hash"],
            }
            if digest(head) != row["hash"]:
                return False, row["seq"], "row hash mismatch (payload or header altered)"
            prev = row["hash"]
        return True, None, "chain intact"

    def trace(self, action_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, ts, kind, payload, hash FROM ledger WHERE action_id = ? ORDER BY seq", (action_id,)
        ).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                 "payload": json.loads(r["payload"]), "hash": r["hash"]} for r in rows]

    def trace_mandate(self, mandate_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, ts, kind, action_id, payload, hash FROM ledger WHERE mandate_id = ? ORDER BY seq",
            (mandate_id,),
        ).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "action_id": r["action_id"],
                 "payload": json.loads(r["payload"]), "hash": r["hash"]} for r in rows]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, ts, kind, mandate_id, action_id, payload FROM ledger ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "mandate_id": r["mandate_id"],
                 "action_id": r["action_id"], "payload": json.loads(r["payload"])} for r in rows]

    # ------------------------------------------------------------ budget ledger

    def _ensure_spend(self, c: sqlite3.Connection, mandate_id: str) -> sqlite3.Row:
        c.execute("INSERT OR IGNORE INTO spend (mandate_id) VALUES (?)", (mandate_id,))
        return c.execute("SELECT * FROM spend WHERE mandate_id = ?", (mandate_id,)).fetchone()

    def spend_state(self, mandate_id: str) -> dict[str, int]:
        with self.transaction() as c:
            r = self._ensure_spend(c, mandate_id)
            return {"committed": r["committed_paise"], "reserved": r["reserved_paise"],
                    "txn_count": r["txn_count"], "denial_streak": r["denial_streak"],
                    "breaker_until": r["breaker_until"], "revoked": r["revoked"]}

    def reserve(self, mandate_id: str, amount: int) -> None:
        with self.transaction() as c:
            self._ensure_spend(c, mandate_id)
            c.execute("UPDATE spend SET reserved_paise = reserved_paise + ?, txn_count = txn_count + 1"
                      " WHERE mandate_id = ?", (amount, mandate_id))

    def commit_reservation(self, mandate_id: str, amount: int) -> None:
        with self.transaction() as c:
            self._ensure_spend(c, mandate_id)
            c.execute("UPDATE spend SET reserved_paise = MAX(reserved_paise - ?, 0),"
                      " committed_paise = committed_paise + ? WHERE mandate_id = ?",
                      (amount, amount, mandate_id))

    def release_reservation(self, mandate_id: str, amount: int, *, refund_txn: bool = True) -> None:
        """Failed execution: give the headroom back, and don't count it as a txn."""
        with self.transaction() as c:
            self._ensure_spend(c, mandate_id)
            c.execute("UPDATE spend SET reserved_paise = MAX(reserved_paise - ?, 0)"
                      " WHERE mandate_id = ?", (amount, mandate_id))
            if refund_txn:
                c.execute("UPDATE spend SET txn_count = MAX(txn_count - 1, 0) WHERE mandate_id = ?", (mandate_id,))

    def credit_refund(self, mandate_id: str, amount: int) -> None:
        """Compensating refund restores budget headroom but keeps the txn count."""
        with self.transaction() as c:
            self._ensure_spend(c, mandate_id)
            c.execute("UPDATE spend SET committed_paise = MAX(committed_paise - ?, 0) WHERE mandate_id = ?",
                      (amount, mandate_id))

    def revoke_mandate(self, mandate_id: str) -> None:
        with self.transaction() as c:
            self._ensure_spend(c, mandate_id)
            c.execute("UPDATE spend SET revoked = 1 WHERE mandate_id = ?", (mandate_id,))

    # ------------------------------------------------------------ nonce cache

    def nonce_seen(self, scope: str, nonce: str, ttl_s: int) -> bool:
        """Atomic check-and-set. True means REPLAY (already present)."""
        cutoff = now_s() - ttl_s
        with self.transaction() as c:
            c.execute("DELETE FROM nonces WHERE seen_at < ?", (cutoff,))
            try:
                c.execute("INSERT INTO nonces (scope, nonce, seen_at) VALUES (?,?,?)", (scope, nonce, now_s()))
            except sqlite3.IntegrityError:
                return True
            return False

    # ------------------------------------------------------------- velocity

    def rate_count(self, scope: str, window_s: int = 60) -> int:
        cutoff = now_s() - window_s
        row = self._conn.execute("SELECT COUNT(*) n FROM rate_events WHERE scope = ? AND ts >= ?",
                                 (scope, cutoff)).fetchone()
        return int(row["n"])

    def rate_record(self, scope: str) -> None:
        with self.transaction() as c:
            c.execute("DELETE FROM rate_events WHERE ts < ?", (now_s() - 3600,))
            c.execute("INSERT INTO rate_events (scope, ts) VALUES (?,?)", (scope, now_s()))

    def note_denial(self, mandate_id: str, threshold: int, cooldown_s: int) -> None:
        with self.transaction() as c:
            self._ensure_spend(c, mandate_id)
            c.execute("UPDATE spend SET denial_streak = denial_streak + 1 WHERE mandate_id = ?", (mandate_id,))
            r = c.execute("SELECT denial_streak FROM spend WHERE mandate_id = ?", (mandate_id,)).fetchone()
            if r["denial_streak"] >= threshold:
                c.execute("UPDATE spend SET breaker_until = ?, denial_streak = 0 WHERE mandate_id = ?",
                          (now_s() + cooldown_s, mandate_id))

    def note_success(self, mandate_id: str) -> None:
        with self.transaction() as c:
            self._ensure_spend(c, mandate_id)
            c.execute("UPDATE spend SET denial_streak = 0 WHERE mandate_id = ?", (mandate_id,))

    # ---------------------------------------------------------- idempotency

    def idem_get(self, key: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()
        if not r:
            return None
        return {"key": r["key"], "state": r["state"], "action_id": r["action_id"],
                "mandate_id": r["mandate_id"], "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "result": json.loads(r["result"]) if r["result"] else None}

    def idem_claim(self, key: str, action_id: str, mandate_id: str, stale_after_s: int = 120) -> tuple[bool, dict | None]:
        """Try to claim the key. Returns (claimed, existing_record).

        A row stuck in `in_flight` past `stale_after_s` is reclaimed — a crashed
        worker must not wedge a cart forever. The reclaim is logged by the caller.
        """
        with self.transaction() as c:
            existing = self.idem_get(key)
            if existing is None:
                c.execute("INSERT INTO idempotency (key, state, action_id, mandate_id, created_at, updated_at)"
                          " VALUES (?,?,?,?,?,?)", (key, "in_flight", action_id, mandate_id, now_s(), now_s()))
                return True, None
            if existing["state"] == "in_flight" and now_s() - existing["updated_at"] > stale_after_s:
                c.execute("UPDATE idempotency SET action_id = ?, updated_at = ? WHERE key = ?",
                          (action_id, now_s(), key))
                return True, existing
            return False, existing

    def idem_finish(self, key: str, state: str, result: dict[str, Any] | None) -> None:
        with self.transaction() as c:
            c.execute("UPDATE idempotency SET state = ?, result = ?, updated_at = ? WHERE key = ?",
                      (state, json.dumps(result) if result is not None else None, now_s(), key))

    def idem_release(self, key: str) -> None:
        """Only for pre-execution aborts — never after a provider call."""
        with self.transaction() as c:
            c.execute("DELETE FROM idempotency WHERE key = ? AND state = 'in_flight'", (key,))

    # ---------------------------------------------------------- capabilities

    def capability_put(self, token: str, payload: dict[str, Any], expires_at: int) -> None:
        with self.transaction() as c:
            c.execute("INSERT INTO capabilities (token, payload, spent, expires_at) VALUES (?,?,0,?)",
                      (token, canonical_bytes(payload).decode(), expires_at))

    def capability_spend(self, token: str) -> tuple[bool, dict[str, Any] | None, str]:
        """Atomically burn a capability. Returns (ok, payload, reason)."""
        with self.transaction() as c:
            r = c.execute("SELECT * FROM capabilities WHERE token = ?", (token,)).fetchone()
            if not r:
                return False, None, "unknown_capability"
            payload = json.loads(r["payload"])
            if r["spent"]:
                return False, payload, "already_spent"
            if r["expires_at"] < now_s():
                return False, payload, "expired"
            c.execute("UPDATE capabilities SET spent = 1 WHERE token = ? AND spent = 0", (token,))
            if c.execute("SELECT changes() ch").fetchone()["ch"] != 1:
                return False, payload, "already_spent"
            return True, payload, "ok"

    # ----------------------------------------------------------------- flags

    def flag_set(self, name: str, value: str) -> None:
        with self.transaction() as c:
            c.execute("INSERT INTO flags (name, value) VALUES (?,?)"
                      " ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value))

    def flag_get(self, name: str, default: str = "") -> str:
        r = self._conn.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return r["value"] if r else default
