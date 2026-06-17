"""
Prometeu Gateway — model-aware request router.

The gateway is a thin entry point: it terminates HTTPS for browsers/SDKs and
routes each /v1 request to a peer that is actually serving the requested model.
The local master is just one peer among many (it advertises itself through the
same registry heartbeat).

Selection is deterministic and explainable (no hidden fallback):
  1. Collect online peers whose `inference.models` contains a ready entry for
     the requested model_id.
  2. Rank by (ready DESC, free_ram_mb DESC, age_sec ASC) — prefer healthy,
     resource-rich, freshly-seen peers.
  3. Return the chosen endpoint + node_id, or None if nobody serves it.

If no peer serves the model, the caller must return an explicit 503 telling the
user to request a pool start — NOT silently route to whatever is loaded.
"""
from __future__ import annotations

from typing import Any, Optional


def _model_matches(requested: str, candidate: str) -> bool:
    if not requested or not candidate:
        return False
    if requested == candidate:
        return True
    # Tolerate common aliasing: "qwen" matches "qwen2.5-1.5b-q4", and a bare
    # repo id matches "repo/file.gguf". Keep this conservative.
    r = requested.lower()
    c = candidate.lower()
    return r in c or c in r


def select_peer_for_model(
    requested_model: str,
    registry_nodes: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Pick the best online peer serving `requested_model`.

    Returns {"node_id", "endpoint", "model_id", "display_name"} or None.
    """
    candidates: list[dict[str, Any]] = []
    for n in registry_nodes:
        if not n.get("online"):
            continue
        inf = n.get("inference") or {}
        for m in inf.get("models", []):
            if not _model_matches(requested_model, m.get("model_id", "")):
                continue
            if not m.get("ready"):
                continue
            endpoint = m.get("endpoint")
            if not endpoint:
                continue
            hw = n.get("hardware") or {}
            tel = hw.get("telemetry") or {}
            free_ram = tel.get("ram_available_mb") or hw.get("ram_available_mb") or 0
            candidates.append({
                "node_id": n.get("node_id"),
                "display_name": n.get("display_name"),
                "endpoint": endpoint.rstrip("/"),
                "model_id": m.get("model_id"),
                "ready": bool(m.get("ready")),
                "free_ram_mb": free_ram,
                "age_sec": n.get("age_sec", 9999),
            })
    if not candidates:
        return None
    candidates.sort(key=lambda c: (not c["ready"], -c["free_ram_mb"], c["age_sec"]))
    return candidates[0]


def list_served_models(registry_nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate which models are being served, by how many ready peers."""
    out: dict[str, dict[str, Any]] = {}
    for n in registry_nodes:
        if not n.get("online"):
            continue
        inf = n.get("inference") or {}
        for m in inf.get("models", []):
            mid = m.get("model_id")
            if not mid:
                continue
            entry = out.setdefault(mid, {"model_id": mid, "ready_peers": 0, "total_peers": 0, "node_ids": []})
            entry["total_peers"] += 1
            if m.get("ready"):
                entry["ready_peers"] += 1
            entry["node_ids"].append(n.get("node_id"))
    return out
