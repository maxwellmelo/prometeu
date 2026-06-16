"""
Prometeu Gateway — pool orchestration (Fase 4).

A *pool* is a set of peers cooperating to serve one (model_id, source). The
orchestrator drives a pool through an explicit state machine and never silently
substitutes a different model or a smaller pool than the sizer requires.

State machine:

    REQUESTED ──> WARMING ──> READY ──> DRAINING ──> STOPPED
                     │           │
                     ├─> FAILED  └─> DEGRADED ──> (back to WARMING or DRAINING)
                     │
                     └─> FAILED (no candidate peers / load errors)

Transitions are computed by pure functions here; I/O (Redis persistence, peer
HTTP calls) lives in the gateway and is injected. This keeps the machine unit
-testable with no infrastructure.

Quorum (`min_peers`) comes from the GGUF sizer (Fase 3) — never hardcoded.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# --- states -----------------------------------------------------------------
REQUESTED = "REQUESTED"
WARMING = "WARMING"
READY = "READY"
DEGRADED = "DEGRADED"
DRAINING = "DRAINING"
STOPPED = "STOPPED"
FAILED = "FAILED"

TERMINAL = {STOPPED, FAILED}
WARMING_TIMEOUT_SEC = 600  # 10 min to reach quorum before FAILED


@dataclass
class Pool:
    pool_id: str
    model_id: str
    source: str
    context: int
    min_peers: int
    state: str = REQUESTED
    members: list[str] = field(default_factory=list)   # node_ids asked to load
    ready_members: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_error: Optional[str] = None
    gguf_url: Optional[str] = None
    sha256: Optional[str] = None
    ram_per_peer_mb: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Pool":
        known = {k: d.get(k) for k in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**known)


def make_pool_id(model_id: str, source: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in f"{source}-{model_id}").strip("-")
    return safe.lower()[:120]


def reconcile(pool: Pool, ready_node_ids: set[str], now: Optional[float] = None) -> Pool:
    """Pure transition: given who is currently ready, advance the pool state.

    `ready_node_ids` = node_ids (among pool.members) whose heartbeat reports the
    pool's model as ready. Caller supplies it from the live registry.
    """
    now = now or time.time()
    ready = [m for m in pool.members if m in ready_node_ids]
    pool.ready_members = ready
    n_ready = len(ready)

    if pool.state in TERMINAL:
        return pool  # no automatic resurrection

    if pool.state == DRAINING:
        if n_ready == 0:
            pool.state = STOPPED
        pool.updated_at = now
        return pool

    # REQUESTED / WARMING / READY / DEGRADED share quorum logic.
    if n_ready >= pool.min_peers:
        pool.state = READY
        pool.last_error = None
    else:
        if pool.state == READY:
            # We lost quorum.
            pool.state = DEGRADED
            pool.last_error = f"lost quorum: {n_ready}/{pool.min_peers} ready"
        elif pool.state in (REQUESTED, WARMING, DEGRADED):
            # Still trying to warm up.
            if pool.state == REQUESTED:
                pool.state = WARMING
            elapsed = now - pool.created_at
            if pool.state == WARMING and elapsed > WARMING_TIMEOUT_SEC:
                pool.state = FAILED
                pool.last_error = (
                    f"warming timed out after {int(elapsed)}s "
                    f"({n_ready}/{pool.min_peers} ready)"
                )
    pool.updated_at = now
    return pool


def select_warm_candidates(
    registry_nodes: list[dict[str, Any]],
    ram_per_peer_mb: int,
    want: int,
    exclude: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Pick online peers with enough free RAM to host one shard.

    Returns up to `want` peer dicts, richest-RAM first. No fallback to peers
    that don't meet the RAM requirement — an under-provisioned pool stays
    under-quorum and surfaces as DEGRADED/FAILED rather than silently OOMing.
    """
    exclude = exclude or set()
    cands: list[tuple[int, dict[str, Any]]] = []
    for n in registry_nodes:
        if not n.get("online"):
            continue
        nid = n.get("node_id")
        if nid in exclude:
            continue
        hw = n.get("hardware") or {}
        tel = hw.get("telemetry") or {}
        free = tel.get("ram_available_mb") or hw.get("ram_available_mb") or 0
        limit = (n.get("limits") or {}).get("ram_mb")
        usable = min(free, limit) if limit else free
        if usable >= ram_per_peer_mb:
            cands.append((int(usable), n))
    cands.sort(key=lambda t: -t[0])
    return [n for _, n in cands[:want]]


__all__ = [
    "Pool", "make_pool_id", "reconcile", "select_warm_candidates",
    "REQUESTED", "WARMING", "READY", "DEGRADED", "DRAINING", "STOPPED", "FAILED",
    "TERMINAL", "WARMING_TIMEOUT_SEC",
]
