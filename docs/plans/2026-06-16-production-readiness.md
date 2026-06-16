# Prometeu Production Readiness Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Turn Prometeu from current community-visible demo into a production-ready, tested inference network where external nodes can safely serve verified GGUF models, pools are observable, failures are explicit, and public UX reflects only real capabilities.

**Architecture:** Keep CT300 gateway as managed coordinator. Keep `/api/nodes` as legacy cluster-debug only. Use `/api/registry/*` for real public capacity. Nodes run `prometeu-node` with sandboxed `llama-server`, hash-verified model loads, heartbeat readiness, and no silent fallback. Gateway sizes models, selects peers, instructs load/unload, reconciles pool state from readiness, and routes `/v1/*` only to ready served models.

**Tech Stack:** FastAPI, Redis, pytest, node-agent Python, systemd/cgroups, llama.cpp binaries, Cloudflare Tunnel, static HTML/CSS/JS.

---

## Current State Audit

Confirmed in repo:

- `gateway/app.py` already has:
  - `NodeJoin.inference`
  - `/api/registry/summary`
  - `/api/pools/request`, `/api/pools`, `/api/pools/{id}/stop`
  - `/api/auth/challenge`, `/api/reciprocity/standing`
  - allowlist gate via `gateway/allowlist.py`
  - sizer integration via `gateway/sizer.py`
- `node/prometeu_node/main.py` already has:
  - `dashboard_url` auto-LAN fallback in heartbeat
  - `POST /api/node/load` async 202 background load
  - `POST /api/node/unload`
  - `GET /api/node/preflight`
- `node/prometeu_node/inference.py` already has:
  - sha256 verification
  - systemd-run sandbox lifecycle
- Tests already exist:
  - `tests/test_sizer.py`
  - `tests/test_pools.py`
  - `tests/test_router.py`
  - `tests/test_reciprocity.py`
  - `tests/test_allowlist.py`
- Current public deploy validated:
  - `4/4` registry nodes online
  - `38` logical cores
  - `38.7GB` RAM
  - `1` model live

Critical gaps found:

1. `node/install.sh` violates no-fallback policy: if llama binary download fails it logs “continuing without llama binaries”. Production must fail install or mark hard blocker, not claim successful inference-capable install.
2. `node/install.sh` runs `/usr/local/bin/prometeu-node-apply-limits || true`, masking resource-limit failure.
3. Need integration tests around gateway → node load contract, especially 202 acceptance and readiness via heartbeat.
4. Need public e2e validation in clean CT/VM, not host.
5. Need routing hardening: `/v1/*` must not silently use legacy local model when requested model is only advertised but not ready.
6. Need release binary asset validation for `prometeu-llama-<target>.tar.gz` before claiming public one-liner works.
7. Need watchdog/self-monitoring for gateway, registry, pool quorum, node heartbeat, and model readiness.
8. Need docs/UX to expose exact states: available capacity vs serving model vs legacy cluster.

---

## Phase 1 — Make installer honest and testable

### Task 1.1: Make llama binary install failure hard-stop

**Objective:** If prebuilt llama binaries are missing, installer exits non-zero and does not present node as inference-capable.

**Files:**
- Modify: `node/install.sh:159-163`
- Test: `tests/test_node_installer_policy.py`

**Steps:**
1. Add pytest that reads `node/install.sh` and asserts it does not contain `continuing without llama binaries` and does not call `install_llama_binaries ||`.
2. Change line 162 to `install_llama_binaries` with normal `set -e` failure behavior.
3. Run `pytest tests/test_node_installer_policy.py -q`.
4. Run full `pytest tests/ -q`.
5. Commit: `fix(node): hard fail when llama binaries are unavailable`.

### Task 1.2: Stop masking resource limit application failure

**Objective:** Resource limits must apply or installer must fail loudly.

**Files:**
- Modify: `node/install.sh:170`
- Test: `tests/test_node_installer_policy.py`

**Steps:**
1. Extend test to assert `prometeu-node-apply-limits || true` is absent.
2. Replace with `/usr/local/bin/prometeu-node-apply-limits`.
3. Run tests.
4. Commit: `fix(node): fail install when resource limits cannot apply`.

### Task 1.3: Add node preflight policy docs in installer output

**Objective:** Install output must distinguish registration from serving readiness.

**Files:**
- Modify: `node/install.sh`
- Modify: `node/README.md`

**Steps:**
1. Add final output line telling operator to check `/api/node/preflight` and `/api/status.resource_limits.applied`.
2. README: document failure semantics: no binary = install fails; no sandbox = node refuses serve.
3. Commit: `docs(node): document hard preflight requirements`.

---

## Phase 2 — Gateway/node load contract tests

### Task 2.1: Unit-test `_node_inference_endpoint`

**Objective:** Coordinator resolves node control endpoint from `dashboard_url` exactly.

**Files:**
- Test: `tests/test_gateway_node_control.py`

**Steps:**
1. Test node with `dashboard_url: http://10.10.10.50:8787` resolves that URL.
2. Test missing endpoint returns `None`.
3. Mock `_list_registry_nodes` in `gateway.app`.
4. Run test.
5. Commit: `test(gateway): cover node control endpoint resolution`.

### Task 2.2: Unit-test `_instruct_peer_load` accepts 202

**Objective:** Gateway treats `202 {status:loading}` from node as accepted.

**Files:**
- Test: `tests/test_gateway_node_control.py`

**Steps:**
1. Mock `_node_inference_endpoint` to return local fake endpoint.
2. Mock `httpx.AsyncClient.post` returning status 202.
3. Assert `_instruct_peer_load(...) is True`.
4. Assert payload includes `model_id`, `gguf_url`, `sha256`, `ctx_size`.
5. Commit: `test(gateway): accept async node load response`.

### Task 2.3: Unit-test no hash means no load

**Objective:** Gateway never instructs a peer to download unverified weights.

**Files:**
- Test: `tests/test_gateway_node_control.py`

**Steps:**
1. Build pool with `sha256=None`.
2. Assert `_instruct_peer_load` returns `False` without HTTP call.
3. Commit: `test(gateway): block unverified peer load instructions`.

---

## Phase 3 — Real pool routing path

### Task 3.1: Make route selection explicit for served model

**Objective:** `/v1/chat/completions` routes only to a ready peer for requested model, or returns 503 `model_not_served`.

**Files:**
- Modify: `gateway/router.py`
- Modify: `gateway/app.py`
- Test: `tests/test_router.py`

**Steps:**
1. Audit existing `select_peer_for_model` behavior.
2. Add/adjust tests for ready vs not ready model.
3. Ensure no fallback to local legacy model for unknown requested model.
4. Commit: `fix(gateway): hard fail when requested model is not served`.

### Task 3.2: Route OpenAI request to selected peer endpoint

**Objective:** When a pool is READY and peer advertises endpoint, gateway proxies `/v1/*` to that peer instead of legacy local `llama-server`.

**Files:**
- Modify: `gateway/app.py`
- Test: `tests/test_gateway_routing.py`

**Steps:**
1. Add pure helper that maps selected peer heartbeat to OpenAI-compatible base URL.
2. Unit-test endpoint extraction from `inference.models[].endpoint`.
3. Wire proxy target selection in chat completion path.
4. Preserve legacy qwen path only when model matches current legacy local model and no pool peer is selected.
5. Commit: `feat(gateway): proxy requests to ready pool peers`.

---

## Phase 4 — Clean CT/VM node validation

### Task 4.1: Create CT validation script

**Objective:** Validate public installer in an isolated CT/VM, never on Proxmox host.

**Files:**
- Create: `scripts/validate-public-node-install.sh`

**Steps:**
1. Script accepts CT ID.
2. Removes old `/opt/prometeu-node`, `/etc/prometeu-node`, `/var/lib/prometeu-node`, service, binaries inside CT.
3. Runs public one-liner inside CT.
4. Checks:
   - `systemctl is-active prometeu-node`
   - `curl :8787/api/status`
   - `resource_limits.applied == true`
   - `/api/node/preflight.can_serve == true`
5. Commit: `test(node): add isolated public install validator`.

### Task 4.2: Build/release binary asset check

**Objective:** Validate release assets exist for every detected target before installer claims support.

**Files:**
- Create: `scripts/check-release-assets.sh`
- Modify: `node/install.sh` if needed

**Steps:**
1. Check GitHub release has `prometeu-llama-linux-x86_64-sandybridge.tar.gz`, `avx2`, `avx512`, `cuda12`, `linux-aarch64` or explicitly mark unsupported.
2. Fail if current host target asset missing.
3. Commit: `ci(node): verify prebuilt llama release assets`.

---

## Phase 5 — Pool lifecycle e2e

### Task 5.1: Add local fake node e2e harness

**Objective:** Test gateway pool lifecycle without downloading a real GGUF.

**Files:**
- Create: `tests/e2e/fake_node.py`
- Create: `tests/test_pool_lifecycle_e2e.py`

**Steps:**
1. Fake node exposes `/api/node/load` returns 202.
2. Fake heartbeat transitions from not ready to ready.
3. Gateway pool request goes REQUESTED → WARMING → READY.
4. Stop goes DRAINING → STOPPED.
5. Commit: `test(e2e): cover pool lifecycle with fake node`.

### Task 5.2: Add real small-model e2e script

**Objective:** Run actual Qwen 0.5B load against CT node and verify `/v1` response through pool.

**Files:**
- Create: `scripts/e2e-real-pool.sh`

**Steps:**
1. Stop old pool if active.
2. Request allowlisted Qwen 0.5B with pinned sha.
3. Poll `/api/pools/<id>` until READY or timeout.
4. Send `/v1/chat/completions` to that exact model.
5. Assert non-empty response and metrics increment.
6. Commit: `test(e2e): add real pool smoke test`.

---

## Phase 6 — Self-monitoring

### Task 6.1: Extend watchdog to cover registry summary

**Objective:** Alert when online nodes drop, serving_now drops, or model live disappears.

**Files:**
- Modify: `scripts/prometeu-pool-watchdog.sh`

**Steps:**
1. Read `/api/registry/summary`.
2. Add streak-based alert for `nodes_online < 3` and `serving_now < 1`.
3. Keep STOPPED pools ignored.
4. Test manually 2 consecutive runs.
5. Commit: `ops: watch registry summary health`.

### Task 6.2: Cron install/update docs

**Objective:** Document watchdog deployment and expected silent/no-agent behavior.

**Files:**
- Modify: `README.md`
- Modify: skill `prometeu` after validation

**Steps:**
1. Add commands to install cron/systemd timer or Hermes cron.
2. Document thresholds.
3. Commit: `docs(ops): document Prometeu watchdog`.

---

## Phase 7 — UX truthfulness and completion criteria

### Task 7.1: Dashboard separates states clearly

**Objective:** UI never implies external nodes are serving if they only report capacity.

**Files:**
- Modify: `web/dashboard.html`
- Modify: `web/chat.html`

**Steps:**
1. Add labels:
   - `Online capacity`
   - `Serving now`
   - `Ready pool models`
   - `Legacy cluster debug` only behind link.
2. Mobile validate no overflow.
3. Commit: `fix(web): label capacity versus serving state`.

### Task 7.2: Final public readiness checklist

**Objective:** Define “done” as verifiable commands, not vibes.

**Files:**
- Create: `docs/production-readiness-checklist.md`

**Required checks:**
1. `pytest tests/ -q` passes.
2. `node/install.sh` succeeds in clean CT with release binaries.
3. `/api/registry/summary` shows expected capacity.
4. `/api/pools/request` for Qwen 0.5B reaches READY.
5. `/v1/chat/completions` returns via selected ready model.
6. `/metrics` exposes gateway + pool + reciprocity metrics.
7. Watchdog silent in healthy state; alerts after streak threshold.
8. Mobile dashboard no horizontal overflow.
9. README one-liner works from clean VM/CT.
10. No unpinned model can load.

Commit: `docs: add production readiness checklist`.

---

## Execution Order

1. Phase 1 first — safe, no production risk, fixes policy violations.
2. Phase 2 — contract tests before routing changes.
3. Phase 3 — production routing behavior.
4. Phase 4 — isolated installer validation.
5. Phase 5 — e2e pool tests.
6. Phase 6 — watchdog/self-monitoring.
7. Phase 7 — UX/docs final polish.

## Abort Gates

- If installer cannot fetch required release binary: stop and build/publish asset; do not bypass.
- If `resource_limits.applied != true`: stop; do not run inference unsandboxed.
- If model sha256 missing/mismatch: stop; do not download.
- If pool cannot reach READY: stop; inspect node logs; do not mark ready manually.
- If `/v1` routes to wrong model: stop; fix router before continuing.

## Final Definition of Done

Prometeu is production-ready when a clean CT/VM can install a node from public one-liner, pass preflight, receive a hash-pinned model load from gateway, report ready via heartbeat, join a READY pool, serve a `/v1/chat/completions` request for that exact model, show correct state in dashboard/mobile, and all automated tests + watchdog checks pass without fallback paths.
