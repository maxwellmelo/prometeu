#!/usr/bin/env bash
# Validate Prometeu public node installer inside an existing Proxmox LXC.
# Host only orchestrates via pct. All destructive cleanup happens inside CT.
set -euo pipefail

CTID="${1:-}"
COORDINATOR_URL="${2:-https://prometeu.mx3dev.com}"
INSTALL_URL="https://raw.githubusercontent.com/maxwellmelo/prometeu/main/node/install.sh"

usage() {
  echo "usage: $0 <ctid> [coordinator_url]" >&2
}

if [[ -z "$CTID" ]]; then
  usage
  exit 2
fi

if [[ ! "$CTID" =~ ^[0-9]+$ ]]; then
  echo "CTID must be numeric" >&2
  exit 2
fi

if ! command -v pct >/dev/null 2>&1; then
  echo "pct not found; run on Proxmox host" >&2
  exit 2
fi

if ! pct status "$CTID" >/dev/null 2>&1; then
  echo "CT $CTID not found" >&2
  exit 2
fi

if ! pct status "$CTID" | grep -q "status: running"; then
  pct start "$CTID"
fi

ct_exec() {
  pct exec "$CTID" -- bash -lc "$1"
}

echo "[prometeu-validator] cleanup old node state inside CT $CTID"
ct_exec '
set -euo pipefail
systemctl disable --now prometu-node prometeu-node 2>/dev/null || true
rm -rf /opt/prometeu-node /etc/prometeu-node /var/lib/prometeu-node
rm -f /etc/systemd/system/prometeu-node.service
rm -f /usr/local/bin/prometeu-node-apply-limits
rm -f /usr/local/bin/llama-server /usr/local/bin/rpc-server
systemctl daemon-reload || true
'

echo "[prometeu-validator] run public installer inside CT $CTID"
pct exec "$CTID" -- bash -lc "curl -fsSL '$INSTALL_URL' | bash -s -- '$COORDINATOR_URL'"

echo "[prometeu-validator] verify systemd service"
ct_exec 'systemctl is-active --quiet prometeu-node'

echo "[prometeu-validator] verify node status + resource limits"
ct_exec 'python3 - <<"PY"
import json
import sys
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8787/api/status", timeout=10) as r:
    status = json.load(r)

resource_limits = status.get("resource_limits") or {}
if resource_limits.get("applied") is not True:
    print(json.dumps({"resource_limits": resource_limits}, indent=2), file=sys.stderr)
    raise SystemExit("resource_limits.applied is not true")
print(json.dumps({"resource_limits.applied": True}, indent=2))
PY'

echo "[prometeu-validator] verify preflight can_serve"
ct_exec 'python3 - <<"PY"
import json
import sys
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8787/api/node/preflight", timeout=10) as r:
    preflight = json.load(r)

if preflight.get("can_serve") is not True:
    print(json.dumps(preflight, indent=2), file=sys.stderr)
    raise SystemExit("/api/node/preflight can_serve is not true")
print(json.dumps({"can_serve": True}, indent=2))
PY'

echo "[prometeu-validator] PASS CT $CTID public node install ready"
