#!/usr/bin/env bash
set -euo pipefail

# Prometeu pool watchdog (Fase 6 hardening)
# - Calls /api/pools, which reconciles pool state against live ready nodes.
# - Emits output only when attention is needed (cron/no_agent can stay silent).
# - No auto-fallback/restart: report facts; operator or orchestrator decides.

BASE_URL="${PROMETEU_GATEWAY_URL:-http://10.10.10.100:3000}"
JSON="$(curl -fsS --max-time 10 "$BASE_URL/api/pools")"

python3 - <<'PY' "$JSON" "$BASE_URL"
import json, sys, time
payload = json.loads(sys.argv[1])
base = sys.argv[2]
bad = []
TERMINAL = {'STOPPED'}
for p in payload.get('pools', []):
    state = p.get('state')
    if state in TERMINAL:
        continue  # intentionally stopped — not an incident
    ready = len(p.get('ready_nodes') or [])
    minp = p.get('min_peers') or 1
    if state in ('FAILED', 'DEGRADED') or ready < minp:
        bad.append(p)

if not bad:
    sys.exit(0)

print(f"PROMETEU WATCHDOG: {len(bad)} pool(s) need attention @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"gateway={base}")
for p in bad:
    print(f"- pool_id={p.get('pool_id')} state={p.get('state')} model={p.get('model_id')} ready={len(p.get('ready_nodes') or [])}/{p.get('min_peers')} requested={p.get('requested_nodes')} ready_nodes={p.get('ready_nodes')}")
PY
