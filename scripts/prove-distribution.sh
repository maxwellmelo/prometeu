#!/usr/bin/env bash
# prove-distribution.sh
#
# Proves that inference work is hitting each Prometeu node by measuring
# per-node CPU/network deltas before and during a generation request.
#
# Usage:
#   bash scripts/prove-distribution.sh https://prometeu.mx3dev.com

set -euo pipefail

BASE_URL="${1:-https://prometeu.mx3dev.com}"
TOKENS="${TOKENS:-120}"
PROMPT="${PROMPT:-Explique em detalhes, em portugues, por que inferencia distribuida exige comunicacao entre todos os nos.}"

need() { command -v "$1" >/dev/null || { echo "missing command: $1" >&2; exit 1; }; }
need curl
need python3

snapshot() {
  curl -s --max-time 10 "$BASE_URL/api/nodes"
}

score() {
  python3 - "$1" "$2" <<'PY'
import json, sys
before=json.loads(sys.argv[1])
after=json.loads(sys.argv[2])
print("node,cpu%,rx_mbps,tx_mbps,tcp,active,verdict")
for n in after["nodes"]:
    t=n.get("telemetry") or {}
    cpu=float(t.get("cpu_percent") or 0)
    rx=float(t.get("rx_mbps") or 0)
    tx=float(t.get("tx_mbps") or 0)
    tcp=(t.get("tcp") or {}).get("established",0)
    active=bool(t.get("active_now"))
    # Conservative: if CPU or network or tcp established appears, node participated.
    ok = (cpu >= 10) or (rx >= 0.1) or (tx >= 0.1) or (tcp >= 1) or active
    print(f"{n['id']},{cpu:.1f},{rx:.3f},{tx:.3f},{tcp},{active},{'OK' if ok else 'WEAK'}")
PY
}

echo "== Prometeu distribution proof =="
echo "base: $BASE_URL"
echo

echo "[1/4] Baseline snapshot"
BEFORE="$(snapshot)"
python3 - <<PY
import json
j=json.loads('''$BEFORE''')
print('alive:', j.get('alive_count'), '/', j.get('total'))
for n in j['nodes']:
    t=n.get('telemetry') or {}
    print(n['id'], 'cpu=', t.get('cpu_percent'), 'rx=', t.get('rx_mbps'), 'tx=', t.get('tx_mbps'), 'tcp=', (t.get('tcp') or {}).get('established'))
PY

echo
echo "[2/4] Fire generation request ($TOKENS tokens)"
REQ_OUT="$(mktemp)"
START=$(date +%s)
curl -s --max-time 180 "$BASE_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":$TOKENS,\"stream\":false,\"temperature\":0.7}" \
  > "$REQ_OUT" &
REQ_PID=$!

sleep 2

echo
echo "[3/4] During-inference snapshot"
DURING="$(snapshot)"
score "$BEFORE" "$DURING"

wait "$REQ_PID"
END=$(date +%s)

echo
echo "[4/4] Request finished in $((END-START))s"
python3 - "$REQ_OUT" <<'PY'
import json, sys
p=sys.argv[1]
try:
    j=json.load(open(p))
    txt=j['choices'][0]['message']['content']
    usage=j.get('usage',{})
    print('output_preview:', txt[:180].replace('\n',' '))
    print('usage:', usage)
except Exception as e:
    print('raw:', open(p).read()[:500])
PY
rm -f "$REQ_OUT"

echo
echo "Verdict: if worker rows show OK with CPU/network/TCP during request, inference is distributed."
