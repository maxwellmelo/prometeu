# Prometeu Node (inference-capable, v0.6.0)

A Prometeu node contributes processing power to the public LLM pool. It:

1. Registers capacity + telemetry with the coordinator (heartbeats every 15s).
2. Exposes a local dashboard on `http://localhost:8787`.
3. On request from the coordinator, **downloads a GGUF, verifies its SHA256, and
   serves it via a sandboxed `llama-server`** (CPU/RAM enforced by systemd cgroups).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/maxwellmelo/prometeu/main/node/install.sh \
  | sudo bash -s -- https://prometeu.mx3dev.com
```

Or from a checkout:

```bash
sudo bash node/install.sh https://prometeu.mx3dev.com
```

The installer:
- detects your CPU/GPU target (`avx2`, `avx512`, `sandybridge`, `cuda12`, `aarch64`)
  and downloads the matching prebuilt `llama-server` + `rpc-server` from GitHub Releases;
- creates a locked-down system user `prometeu-inf` (no shell, no home);
- creates `/var/lib/prometeu-node/models` owned by that user;
- installs and starts the `prometeu-node` systemd service.

## Sandbox model (no fallbacks)

Inference processes never run with the daemon's privileges. Each model is launched
with `systemd-run` as user `prometeu-inf` with:

- `CPUQuota=<limit>%`, `MemoryMax=<limit>M` (cgroup-enforced, from your config limits)
- `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `NoNewPrivileges=true`
- `ReadWritePaths=/var/lib/prometeu-node/models` only

If `systemd-run`, the sandbox user, or the `llama-server` binary is missing, the node
**refuses to serve inference** and reports the blocker. There is no silent fallback to
an unsandboxed process.

Check readiness:

```bash
curl -s http://localhost:8787/api/node/preflight | python3 -m json.tool
```

All four must be true to serve: `llama_server_bin`, `systemd_run`, `sandbox_user`,
`models_dir_writable`.

## Endpoints (local, :8787)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | full node state + telemetry + resource limits |
| GET | `/api/node/preflight` | sandbox readiness check |
| GET | `/api/node/models` | currently-served models + health |
| POST | `/api/node/load` | `{model_id, gguf_url, sha256, ctx_size?, rpc_peers?, cpu_quota?, mem_mb?}` |
| POST | `/api/node/unload` | `{model_id}` |
| POST | `/api/config` | update limits/display_name/etc |

`/api/node/load` and `/api/node/unload` are intended to be called by the coordinator
during pool warming/draining (Fase 4), but are reachable locally for testing.

## Manual binary install (if no prebuilt target matches)

Build llama.cpp yourself (see the main repo README for the Sandy Bridge flags), then:

```bash
sudo install -m0755 llama-server /usr/local/bin/llama-server
sudo install -m0755 rpc-server   /usr/local/bin/rpc-server
sudo systemctl restart prometeu-node
```

## Uninstall

```bash
sudo systemctl disable --now prometeu-node
sudo rm -rf /opt/prometeu-node /etc/prometeu-node /var/lib/prometeu-node
sudo rm -f /usr/local/bin/prometeu-node-apply-limits
sudo userdel prometeu-inf
```
