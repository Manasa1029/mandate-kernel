"""Ledger and state-store properties. Concurrency is tested with real threads,
because the interesting bugs in a payment ledger only appear under contention."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from kernel.models import now_s
from kernel.store import GENESIS, Store


def test_chain_starts_from_genesis(world):
    seq, h = world.store.append("test.event", {"a": 1})
    assert seq == 1 and len(h) == 64
    assert GENESIS == "0" * 64
    ok, bad, _ = world.store.verify_chain()
    assert ok and bad is None


def test_chain_links_every_entry(world):
    hashes = [world.store.append("e", {"n": n})[1] for n in range(20)]
    assert len(set(hashes)) == 20, "identical payloads must still chain to distinct hashes"
    ok, bad, _ = world.store.verify_chain()
    assert ok and bad is None


def test_tampering_is_detected(tmp_path: Path):
    db = str(tmp_path / "k.db")
    store = Store(db)
    for n in range(5):
        store.append("e", {"n": n})
    assert store.verify_chain()[0]

    raw = sqlite3.connect(db)
    raw.execute("UPDATE ledger SET payload = ? WHERE seq = 3", ('{"n": 999}',))
    raw.commit()
    raw.close()

    ok, bad_seq, detail = Store(db).verify_chain()
    assert not ok and bad_seq == 3 and detail


def test_deleting_a_row_is_detected(tmp_path: Path):
    db = str(tmp_path / "k.db")
    store = Store(db)
    for n in range(5):
        store.append("e", {"n": n})
    raw = sqlite3.connect(db)
    raw.execute("DELETE FROM ledger WHERE seq = 3")
    raw.commit()
    raw.close()
    assert not Store(db).verify_chain()[0]


def test_nonce_is_atomic_check_and_set(world):
    assert world.store.nonce_seen("action", "n1", 60) is False
    assert world.store.nonce_seen("action", "n1", 60) is True


def test_nonce_scopes_are_independent(world):
    assert world.store.nonce_seen("action", "n1", 60) is False
    assert world.store.nonce_seen("cart", "n1", 60) is False


def test_idempotency_claim_is_exclusive(world):
    claimed, existing = world.store.idem_claim("k1", "act_1", "mnd_1")
    assert claimed and existing is None
    claimed2, existing2 = world.store.idem_claim("k1", "act_2", "mnd_1")
    assert not claimed2 and existing2 is not None


def test_finished_idempotency_replays_the_stored_result(world):
    world.store.idem_claim("k1", "act_1", "mnd_1")
    world.store.idem_finish("k1", "done", {"provider_id": "order_123"})
    _, existing = world.store.idem_claim("k1", "act_2", "mnd_1")
    assert existing["state"] == "done"
    assert existing["result"]["provider_id"] == "order_123"


def test_released_key_can_be_claimed_again(world):
    world.store.idem_claim("k1", "act_1", "mnd_1")
    world.store.idem_release("k1")
    claimed, _ = world.store.idem_claim("k1", "act_2", "mnd_1")
    assert claimed


def test_concurrent_claims_yield_exactly_one_winner(tmp_path: Path):
    """The property that stops a double charge when two agent replicas race."""
    db = str(tmp_path / "race.db")
    Store(db)  # create schema once
    wins: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        store = Store(db)
        barrier.wait()
        claimed, _ = store.idem_claim("same-key", "act_1", "mnd_1")
        with lock:
            wins.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 1, f"expected exactly one winner, got {sum(wins)}"


def test_concurrent_reservations_are_not_lost(tmp_path: Path):
    db = str(tmp_path / "res.db")
    Store(db)
    barrier = threading.Barrier(10)

    def worker():
        store = Store(db)
        barrier.wait()
        store.reserve("mnd_1", 100)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state = Store(db).spend_state("mnd_1")
    assert state["reserved"] == 1000 and state["txn_count"] == 10


def test_concurrent_appends_keep_the_chain_intact(tmp_path: Path):
    db = str(tmp_path / "chain.db")
    Store(db)
    barrier = threading.Barrier(6)

    def worker(n: int):
        store = Store(db)
        barrier.wait()
        for i in range(10):
            store.append("e", {"w": n, "i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok, bad, detail = Store(db).verify_chain()
    assert ok, f"chain broken at {bad}: {detail}"
    assert len(Store(db).recent(200)) == 60


def test_rate_window_only_counts_the_window(world):
    for _ in range(3):
        world.store.rate_record("mnd_1")
    assert world.store.rate_count("mnd_1", 60) == 3
    assert world.store.rate_count("mnd_1", -1) == 0  # window in the past sees nothing
    assert world.store.rate_count("other_mandate", 60) == 0


def test_release_reservation_never_goes_negative(world):
    world.store.reserve("mnd_1", 100)
    world.store.release_reservation("mnd_1", 100_000)
    assert world.store.spend_state("mnd_1")["reserved"] == 0


def test_trace_is_scoped_to_one_action(world):
    world.store.append("a", {}, mandate_id="m1", action_id="act_1")
    world.store.append("b", {}, mandate_id="m1", action_id="act_2")
    assert [e["kind"] for e in world.store.trace("act_1")] == ["a"]


def test_trace_mandate_returns_the_whole_history(world):
    world.store.append("a", {}, mandate_id="m1", action_id="act_1")
    world.store.append("b", {}, mandate_id="m1", action_id="act_2")
    world.store.append("c", {}, mandate_id="m2", action_id="act_3")
    assert len(world.store.trace_mandate("m1")) == 2


def test_capability_is_burned_exactly_once(world):
    world.store.capability_put("tok_1", {"amount_paise": 100}, expires_at=now_s() + 60)
    ok, payload, _ = world.store.capability_spend("tok_1")
    assert ok and payload["amount_paise"] == 100
    ok2, _, reason = world.store.capability_spend("tok_1")
    assert not ok2 and reason


def test_expired_capability_cannot_be_spent(world):
    world.store.capability_put("tok_1", {"amount_paise": 100}, expires_at=now_s() - 1)
    ok, _, reason = world.store.capability_spend("tok_1")
    assert not ok and "expire" in reason.lower()


def test_unknown_capability_is_refused(world):
    ok, payload, reason = world.store.capability_spend("never_minted")
    assert not ok and payload is None and reason == "unknown_capability"


def test_configured_provider_timeout_reaches_the_rest_client(monkeypatch):
    """KERNEL_PROVIDER_TIMEOUT_S was parsed into config but never passed to the
    factory, so REST calls silently used the adapter default regardless of it."""
    from adapters import build_provider

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_dummy")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "dummy_secret")
    provider = build_provider("rest", timeout=2.5)
    assert provider._client.timeout.read == 2.5
    assert provider._client.timeout.connect == 3.0
    assert build_provider("rest")._client.timeout.read == 8.0
