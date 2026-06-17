"""Tests for the pool orchestration state machine (Fase 4)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

import pools as P


def _pool(min_peers=2, members=None, state=P.REQUESTED, created=None):
    return P.Pool(
        pool_id="hf-test", model_id="test", source="hf", context=2048,
        min_peers=min_peers, members=members or ["a", "b", "c"], state=state,
        created_at=created or time.time(),
    )


def test_make_pool_id_safe():
    pid = P.make_pool_id("Qwen/Qwen2.5-1.5B/q4.gguf", "hf")
    assert "/" not in pid and pid == pid.lower()


def test_requested_to_warming_then_ready():
    p = _pool(min_peers=2)
    P.reconcile(p, set())            # nobody ready yet
    assert p.state == P.WARMING
    P.reconcile(p, {"a", "b"})       # quorum reached
    assert p.state == P.READY
    assert set(p.ready_members) == {"a", "b"}


def test_ready_loses_quorum_becomes_degraded():
    p = _pool(min_peers=2, state=P.READY)
    P.reconcile(p, {"a"})            # only 1 ready now
    assert p.state == P.DEGRADED
    assert "lost quorum" in (p.last_error or "")


def test_degraded_recovers_to_ready():
    p = _pool(min_peers=2, state=P.DEGRADED)
    P.reconcile(p, {"a", "b"})
    assert p.state == P.READY
    assert p.last_error is None


def test_warming_timeout_fails():
    p = _pool(min_peers=3, state=P.WARMING, created=time.time() - (P.WARMING_TIMEOUT_SEC + 10))
    P.reconcile(p, {"a"})
    assert p.state == P.FAILED
    assert "timed out" in (p.last_error or "")


def test_warming_uses_per_pool_deadline_when_set():
    # A large-model pool sets an explicit warming_deadline_sec well above the
    # static default. It must NOT fail at the static default — download time
    # for big GGUFs on slow links legitimately exceeds 600s.
    p = _pool(min_peers=2, state=P.WARMING,
              created=time.time() - (P.WARMING_TIMEOUT_SEC + 60))
    p.warming_deadline_sec = P.WARMING_TIMEOUT_SEC + 1200
    P.reconcile(p, set())
    assert p.state == P.WARMING  # still within its own deadline


def test_warming_fails_past_per_pool_deadline():
    p = _pool(min_peers=2, state=P.WARMING)
    p.warming_deadline_sec = 300
    p.created_at = time.time() - 360
    P.reconcile(p, set())
    assert p.state == P.FAILED
    assert "timed out" in (p.last_error or "")


def test_warming_deadline_for_size_scales_with_bytes():
    small = P.warming_deadline_for_size(file_size_bytes=200_000_000, min_peers=1)
    large = P.warming_deadline_for_size(file_size_bytes=1_900_000_000, min_peers=2)
    assert large > small
    # Never below the static floor.
    assert small >= P.WARMING_TIMEOUT_SEC


def test_draining_to_stopped_when_empty():
    p = _pool(state=P.DRAINING)
    P.reconcile(p, {"a"})            # still one ready -> stays draining
    assert p.state == P.DRAINING
    P.reconcile(p, set())            # all gone -> stopped
    assert p.state == P.STOPPED


def test_terminal_states_frozen():
    for st in (P.STOPPED, P.FAILED):
        p = _pool(state=st)
        P.reconcile(p, {"a", "b", "c"})
        assert p.state == st         # no resurrection


def test_select_candidates_respects_ram_and_limits():
    nodes = [
        {"node_id": "a", "online": True, "hardware": {"telemetry": {"ram_available_mb": 4000}}},
        {"node_id": "b", "online": True, "hardware": {"telemetry": {"ram_available_mb": 8000}}, "limits": {"ram_mb": 1000}},
        {"node_id": "c", "online": True, "hardware": {"telemetry": {"ram_available_mb": 6000}}},
        {"node_id": "d", "online": False, "hardware": {"telemetry": {"ram_available_mb": 9000}}},
    ]
    # need 2000MB/peer: a(4000) and c(6000) qualify; b capped to 1000; d offline
    chosen = P.select_warm_candidates(nodes, ram_per_peer_mb=2000, want=3)
    ids = [n["node_id"] for n in chosen]
    assert ids == ["c", "a"]         # richest-first, b/d excluded


def test_roundtrip_serialization():
    p = _pool()
    d = p.to_dict()
    p2 = P.Pool.from_dict(d)
    assert p2.pool_id == p.pool_id and p2.min_peers == p.min_peers
