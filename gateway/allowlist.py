"""
Prometeu Gateway — curated model allowlist (Fase 6 hardening).

Primary defense against poisoned-model attacks. A pool may only load a model
that appears in the curated allowlist. Two enforcement layers:

  1. Gateway side (here): /api/pools/request rejects any model_id not on the
     list with 403. If the list pins a sha256, the request must match it.
  2. Peer side (node/inference.py): the node verifies the downloaded GGUF's
     sha256 before serving. A mismatch => refuse to serve (no fallback).

The allowlist is loaded from allowlist.json next to this module (override with
PROMETEU_ALLOWLIST). Entries with an empty sha256 are "trust on first verified
download" — the gateway accepts a caller-supplied sha256 and the operator pins
it afterwards; this is logged so unpinned entries are visible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

_DEFAULT_PATH = Path(__file__).with_name("allowlist.json")


def _path() -> Path:
    return Path(os.getenv("PROMETEU_ALLOWLIST", str(_DEFAULT_PATH)))


def load_allowlist() -> dict[str, Any]:
    p = _path()
    if not p.is_file():
        return {"schema": "prometeu/allowlist/1", "models": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"schema": "prometeu/allowlist/1", "models": []}


def find_entry(model_id: str, source: str = "hf") -> Optional[dict[str, Any]]:
    data = load_allowlist()
    for m in data.get("models", []):
        if m.get("model_id") == model_id and m.get("source", "hf") == source:
            return m
    return None


def check_model(
    model_id: str,
    source: str,
    supplied_sha256: Optional[str],
) -> dict[str, Any]:
    """Decide whether a pool may load this model.

    Returns {"allowed": bool, "reason": str, "sha256": <effective sha or None>,
             "pinned": bool}.

    Rules (no fallback to "just trust it"):
      - model not on allowlist  -> denied.
      - allowlist pins a sha256 -> caller's sha (if any) must match it; the
        pinned value is authoritative.
      - allowlist sha empty     -> require the caller to supply one (so the peer
        can verify the download); accept it, mark unpinned for operator review.
    """
    entry = find_entry(model_id, source)
    if not entry:
        return {"allowed": False, "reason": "model not on curated allowlist", "sha256": None, "pinned": False}

    pinned = (entry.get("sha256") or "").strip().lower()
    supplied = (supplied_sha256 or "").strip().lower()

    if pinned:
        if supplied and supplied != pinned:
            return {"allowed": False, "reason": "sha256 mismatch vs pinned allowlist value", "sha256": pinned, "pinned": True}
        return {"allowed": True, "reason": "ok (pinned)", "sha256": pinned, "pinned": True}

    # Not yet pinned: require a caller-supplied sha256 so the peer can verify.
    if not supplied:
        return {"allowed": False, "reason": "allowlist entry has no pinned sha256; supply sha256 so peers can verify the download", "sha256": None, "pinned": False}
    return {"allowed": True, "reason": "ok (unpinned; operator should pin)", "sha256": supplied, "pinned": False}


__all__ = ["load_allowlist", "find_entry", "check_model"]
