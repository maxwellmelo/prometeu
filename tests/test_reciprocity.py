"""Tests for reciprocity & signed-challenge auth (Fase 5)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

import base64
import reciprocity as R
from nacl.signing import SigningKey


def _keypair():
    sk = SigningKey.generate()
    pub_b64 = base64.b64encode(bytes(sk.verify_key)).decode()
    return sk, pub_b64


def test_verify_good_signature():
    sk, pub = _keypair()
    nonce = R.make_nonce()
    sig = base64.b64encode(sk.sign(nonce.encode()).signature).decode()
    assert R.verify_signature(pub, nonce, sig) is True


def test_verify_rejects_tampered_nonce():
    sk, pub = _keypair()
    nonce = R.make_nonce()
    sig = base64.b64encode(sk.sign(nonce.encode()).signature).decode()
    assert R.verify_signature(pub, nonce + "x", sig) is False


def test_verify_rejects_wrong_key():
    sk, _ = _keypair()
    _, other_pub = _keypair()
    nonce = R.make_nonce()
    sig = base64.b64encode(sk.sign(nonce.encode()).signature).decode()
    assert R.verify_signature(other_pub, nonce, sig) is False


def test_verify_rejects_garbage():
    assert R.verify_signature("notbase64!!", "n", "alsogarbage") is False


def test_nonce_unique():
    assert R.make_nonce() != R.make_nonce()


def test_standing_neutral_when_no_consumption():
    assert R.standing(0, 0) == 0.0
    assert R.standing(2000, 0) == 2.0  # fresh contributor gets credit


def test_standing_ratio():
    assert R.standing(1000, 500) == 2.0
    assert R.standing(250, 1000) == 0.25


def test_rpm_tiers():
    assert R.rpm_for_standing(0.0, authenticated=False) == R.ANON_RPM
    assert R.rpm_for_standing(0.5, authenticated=True) == 30
    assert R.rpm_for_standing(1.0, authenticated=True) == 60
    assert R.rpm_for_standing(10.0, authenticated=True) == 120
    assert R.rpm_for_standing(50.0, authenticated=True) == 300


def test_quota_soft_block_only_over_limit():
    # anonymous floor = ANON_RPM
    d = R.quota_decision(False, 0, 0, used_this_minute=R.ANON_RPM - 1)
    assert d["allow"] is True
    d2 = R.quota_decision(False, 0, 0, used_this_minute=R.ANON_RPM)
    assert d2["allow"] is False
    assert d2["retry_after_sec"] == 60


def test_contributor_gets_more_headroom():
    anon = R.quota_decision(False, 0, 0, 25)
    contrib = R.quota_decision(True, 100000, 1000, 25)  # standing 100 -> 300 rpm
    assert anon["allow"] is False      # over 20
    assert contrib["allow"] is True    # under 300
