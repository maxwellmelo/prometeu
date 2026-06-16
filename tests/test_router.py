"""Tests for the gateway model router (Fase 2)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

from router import select_peer_for_model, list_served_models, _model_matches


def _node(node_id, online, models, ram=2000, age=5.0, name=None):
    return {
        "node_id": node_id,
        "display_name": name or node_id,
        "online": online,
        "age_sec": age,
        "hardware": {"telemetry": {"ram_available_mb": ram}},
        "inference": {"models": models},
    }


def test_match_exact_and_loose():
    assert _model_matches("qwen2.5-1.5b-q4", "qwen2.5-1.5b-q4")
    assert _model_matches("qwen", "qwen2.5-1.5b-q4")
    assert _model_matches("qwen2.5-1.5b-q4", "qwen")
    assert not _model_matches("llama3-8b", "qwen2.5-1.5b-q4")
    assert not _model_matches("", "x")


def test_select_prefers_ready_then_ram():
    nodes = [
        _node("a", True, [{"model_id": "llama3-8b", "ready": False, "endpoint": "http://a:18080"}], ram=8000),
        _node("b", True, [{"model_id": "llama3-8b", "ready": True, "endpoint": "http://b:18080"}], ram=4000),
        _node("c", True, [{"model_id": "llama3-8b", "ready": True, "endpoint": "http://c:18080"}], ram=6000),
    ]
    peer = select_peer_for_model("llama3-8b", nodes)
    assert peer["node_id"] == "c"  # ready + most free RAM among ready


def test_select_none_when_unserved():
    nodes = [_node("a", True, [{"model_id": "qwen", "ready": True, "endpoint": "http://a:18080"}])]
    assert select_peer_for_model("llama3-70b", nodes) is None


def test_select_skips_offline():
    nodes = [_node("a", False, [{"model_id": "llama3-8b", "ready": True, "endpoint": "http://a:18080"}])]
    assert select_peer_for_model("llama3-8b", nodes) is None


def test_select_skips_no_endpoint():
    nodes = [_node("a", True, [{"model_id": "llama3-8b", "ready": True, "endpoint": None}])]
    assert select_peer_for_model("llama3-8b", nodes) is None


def test_list_served_counts():
    nodes = [
        _node("a", True, [{"model_id": "llama3-8b", "ready": True, "endpoint": "http://a:1"}]),
        _node("b", True, [{"model_id": "llama3-8b", "ready": False, "endpoint": "http://b:1"}]),
        _node("c", False, [{"model_id": "llama3-8b", "ready": True, "endpoint": "http://c:1"}]),
    ]
    served = list_served_models(nodes)
    assert served["llama3-8b"]["total_peers"] == 2  # c is offline
    assert served["llama3-8b"]["ready_peers"] == 1
