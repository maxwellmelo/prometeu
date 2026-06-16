"""
Prometeu Gateway — reciprocity & signed-challenge auth (Fase 5).

Two cooperating mechanisms, both *soft* by design (decision: never hard-block):

1. SIGNED CHALLENGE (proof-of-key, not TOFU)
   A peer/user with an Ed25519 keypair proves possession of the private key
   matching the `public_key` it registered. Flow:
     - client GETs a challenge (random nonce, short TTL)
     - client signs nonce with its private key
     - client POSTs {public_key, nonce, signature}; gateway verifies and issues
       a short-lived bearer token bound to that public key.
   No private key ever leaves the client. The gateway only stores public keys.

2. RECIPROCITY LEDGER ("serve to consume")
   Contribution is measured by SIGNED RECEIPTS (tokens_served), not uptime.
   Consumption is metered per /v1 request (tokens). A peer's standing is the
   ratio contributed/consumed. Standing maps to a soft rate multiplier: heavy
   contributors get more headroom; pure consumers get the anonymous floor —
   but are never fully blocked (soft-quota).

Crypto verification and the standing->multiplier math are pure functions here;
Redis I/O lives in the gateway.
"""
from __future__ import annotations

import base64
import os
import secrets
import time
from typing import Any, Optional

try:
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError
    _HAVE_NACL = True
except Exception:  # pragma: no cover - dep missing in some envs
    _HAVE_NACL = False


CHALLENGE_TTL_SEC = int(os.getenv("PROMETEU_CHALLENGE_TTL_SEC", "120"))
TOKEN_TTL_SEC = int(os.getenv("PROMETEU_AUTH_TOKEN_TTL_SEC", "3600"))

# Soft-quota tiers: (min_standing, requests_per_minute). Standing = contributed
# tokens / max(consumed tokens, 1). Anonymous (no token) gets ANON_RPM.
ANON_RPM = int(os.getenv("PROMETEU_ANON_RPM", "20"))
TIERS = [
    (0.0, 30),    # authenticated but net consumer
    (1.0, 60),    # gives back as much as it takes
    (5.0, 120),   # net contributor
    (20.0, 300),  # heavy contributor
]


def _b64decode(s: str) -> bytes:
    s = s.strip()
    # tolerate urlsafe and missing padding
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    return base64.b64decode(s + "=" * pad)


def make_nonce() -> str:
    return secrets.token_urlsafe(32)


def verify_signature(public_key_b64: str, nonce: str, signature_b64: str) -> bool:
    """Verify that `signature` over `nonce` was produced by `public_key`.

    Returns False on any malformed input or bad signature. No exceptions leak —
    callers treat False as "auth failed" (no fallback to trusting the claim).
    """
    if not _HAVE_NACL:
        raise RuntimeError("pynacl not installed; signed-challenge auth unavailable")
    try:
        vk = VerifyKey(_b64decode(public_key_b64))
        vk.verify(nonce.encode("utf-8"), _b64decode(signature_b64))
        return True
    except (BadSignatureError, ValueError, TypeError, Exception):
        return False


def make_token() -> str:
    return secrets.token_urlsafe(24)


def standing(contributed_tokens: int, consumed_tokens: int) -> float:
    """Contribution/consumption ratio. New peers (0 consumed) start neutral."""
    consumed = max(int(consumed_tokens), 0)
    contributed = max(int(contributed_tokens), 0)
    if consumed == 0:
        # Hasn't consumed yet: standing equals contribution scaled (so a fresh
        # contributor gets credit, a fresh do-nothing stays at 0).
        return float(contributed) / 1000.0 if contributed else 0.0
    return contributed / consumed


def rpm_for_standing(s: float, authenticated: bool) -> int:
    """Map standing -> requests-per-minute soft cap."""
    if not authenticated:
        return ANON_RPM
    rpm = TIERS[0][1]
    for min_s, val in TIERS:
        if s >= min_s:
            rpm = val
    return rpm


def quota_decision(
    authenticated: bool,
    contributed_tokens: int,
    consumed_tokens: int,
    used_this_minute: int,
) -> dict[str, Any]:
    """Soft quota: returns allow + the limit + a retry hint. Never hard-denies
    an authenticated contributor below their tier; anonymous callers get the
    floor. 'allow' False only means "slow down", surfaced as 429 with Retry-After.
    """
    s = standing(contributed_tokens, consumed_tokens)
    limit = rpm_for_standing(s, authenticated)
    allow = used_this_minute < limit
    return {
        "allow": allow,
        "limit_rpm": limit,
        "standing": round(s, 3),
        "authenticated": authenticated,
        "used_this_minute": used_this_minute,
        "retry_after_sec": 0 if allow else 60,
    }


__all__ = [
    "make_nonce", "verify_signature", "make_token", "standing",
    "rpm_for_standing", "quota_decision",
    "CHALLENGE_TTL_SEC", "TOKEN_TTL_SEC", "ANON_RPM", "TIERS",
]
