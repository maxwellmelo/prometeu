# Prometeu

> Distributed LLM inference on commodity hardware. Free, public, no GPU.

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache_2.0-blue?style=flat-square)](LICENSE)
[![Powered by Prometeu](https://img.shields.io/badge/powered_by-Prometeu-7c3aed?style=flat-square)](https://github.com/maxwellmelo/prometeu)

🔗 **Live demo:** [prometeu.mx3dev.com](https://prometeu.mx3dev.com) — talk to a small open LLM running on a 2011 Intel i7 with no GPU.

🇧🇷 [Versão em português](#prometeu-pt-br)

---

## What is this

**Prometeu** is a Apache 2.0–licensed project for running a single large language model split across many small machines that, individually, cannot host it. The reference cluster runs `Qwen 2.5 1.5B Q4_K_M` split across three Linux containers on an Intel i7-2620M from **2011** — no GPU, 8 GB RAM, no AVX2, no BMI2 — at ~9 tok/s LAN, ~6.7 tok/s over the public internet.

This is not a benchmark king. It is a proof that **distributed LLM inference can run on hardware most people would throw away**, exposed publicly via Cloudflare Tunnel at zero monthly cost.

Three things make it interesting:

1. **It works on hardware where it shouldn't.** Sandy Bridge CPUs lack instructions that modern compilers love to emit silently — we ship the build flags that pin it correctly.
2. **It's split, not replicated.** Each worker hosts a slice of the model's tensor graph. No worker has the whole model in RAM.
3. **The pieces are honest.** Every operational component is open: the FastAPI gateway, the systemd units, the build scripts, the P2P mesh transport, the signed-receipt protocol.

---

## Who is this for

| You are... | Read this section |
|---|---|
| Just want to **try** a free distributed LLM | [Live demo](#live-demo) |
| Want to **host a node** contributing to the public pool | [Run a participating node](#run-a-participating-node) |
| Want to **deploy your own cluster** end-to-end | [Self-host the full stack](#self-host-the-full-stack) |
| Building **on top of** Prometeu | [Attribution requirements](#attribution-requirements) |

---

## Live demo

- **Web chat:** https://prometeu.mx3dev.com
- **OpenAI-compatible API:** `https://prometeu.mx3dev.com/v1/chat/completions`
- **Cluster status:** https://prometeu.mx3dev.com/api/nodes
- **Mesh peers:** https://prometeu.mx3dev.com/api/mesh/peers
- **Model catalog (allowlist):** https://prometeu.mx3dev.com/api/catalog/allowlist
- **Pools:** https://prometeu.mx3dev.com/api/pools
- **Prometheus metrics:** https://prometeu.mx3dev.com/metrics

### OpenAI SDK example

```python
from openai import OpenAI
client = OpenAI(base_url="https://prometeu.mx3dev.com/v1", api_key="not-needed")

stream = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "Why is the sky blue?"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

Public access: **reciprocity soft-quota** on `/v1/*`. Anonymous callers get a floor (~20 req/min per IP); authenticated peers that contribute capacity (signed receipts) get larger quotas. Never a hard block — over-quota returns `429` with `Retry-After`. See [Reciprocity & auth](#reciprocity--auth).

---

## Stack

```
[user] ──TLS──▶ Cloudflare Tunnel ──▶ FastAPI gateway :3000
                                          │
                                          ▼ proxy /v1/*  (rate-limited, metered)
                                      llama-server :8080  (master)
                                          │
                                          ▼ RPC tensors over Iroh P2P mesh
                                rpc-server :50052 (worker 1, layers 9-16)
                                rpc-server :50052 (worker 2, layers 17-27)
```

| Component | Role |
|---|---|
| `llama-server` (master) | OpenAI-compatible HTTP API, loads model header, orchestrates RPC fan-out |
| `rpc-server` (workers) | Hosts a slice of the model's tensor graph, computes forward pass on demand |
| FastAPI gateway | Model-aware routing, GGUF sizer, pool orchestration, reciprocity soft-quota, Ed25519 auth, allowlist gate, Prometheus metrics, mesh discovery, registry |
| `prometeu-mesh` (Rust) | Iroh P2P overlay, Ed25519 identity, TCP bridge, signed CBOR receipts |
| `prometeu-node` daemon | Per-participant dashboard `:8787`, heartbeat into public registry |
| Cloudflare Tunnel | Public HTTPS without opening firewall ports |

---

## Performance, honestly

| Setup | Tokens/sec |
|---|---|
| 3-node split, LAN | ~9.18 |
| 3-node split, over public internet (HTTPS) | ~6.7 |
| First token latency, warm cache | ~0.5s |
| First token latency, cold (post-reboot) | 30–90s |

For comparison: a single modern GPU does ~100 tok/s on the same model. We compete on **dollars spent**, not throughput.

---

## The interesting technical bit

The reference cluster's CPU is **Sandy Bridge** (2011). It has AVX but no AVX2, no BMI2, no FMA, no F16C.

GCC, on `-march=native`, happily emits BMI2 instructions like `shlx` even when you ask `cmake` to disable AVX2 — and llama.cpp's CMake flags alone don't stop this. The binary builds, runs once, and dies with `SIGILL`.

The fix is a belt-and-suspenders compile invocation:

```bash
cmake .. \
    -DGGML_RPC=ON \
    -DGGML_NATIVE=OFF \
    -DGGML_AVX=ON -DGGML_AVX2=OFF \
    -DGGML_FMA=OFF -DGGML_F16C=OFF -DGGML_BMI2=OFF \
    -DCMAKE_C_FLAGS="-march=sandybridge -mno-bmi -mno-bmi2 -mno-avx2 -mno-fma -mno-f16c" \
    -DCMAKE_CXX_FLAGS="-march=sandybridge -mno-bmi -mno-bmi2 -mno-avx2 -mno-fma -mno-f16c"
```

Pinned at `-march=sandybridge`, the binary runs on any Sandy/Ivy Bridge box. Full script: [`scripts/build-llama-cpp.sh`](scripts/build-llama-cpp.sh).

---

## Run a participating node

Want to lend your machine to the public Prometeu pool? Install the participant daemon:

```bash
sudo bash node/install.sh https://prometeu.mx3dev.com
```

It opens a local dashboard at `http://localhost:8787`, detects your hardware (CPU, RAM, disk, GPU/VRAM when available), lets you choose the active LLM from a HuggingFace/Ollama catalog, lets you set participation limits, and heartbeats into the public registry every 15s.

The installer auto-detects your CPU/GPU target (`linux-x86_64-cuda12`, `linux-aarch64`, or a Sandy-Bridge-safe x86_64 bundle), pins the correct llama.cpp binary, and provisions a **sandboxed** inference runner (dedicated user + systemd-run cgroup limits). On a pool request from the coordinator the node downloads the GGUF, **verifies its sha256 against the curated allowlist**, and only then serves it — no fallback if the hash mismatches or sandbox prerequisites are missing (blockers surface at `/api/node/preflight`).

**What works today:**
- ✅ Local dashboard with hardware fingerprint (CPU/RAM/disk + NVIDIA/AMD/DRM GPU detection)
- ✅ Enforced CPU/RAM limits via systemd cgroups (`CPUQuota`, `MemoryMax`) + sandboxed inference user
- ✅ Multi-target llama.cpp bundles selected at install (CUDA12 / aarch64 / Sandy-Bridge-safe), built in CI
- ✅ Model catalog selector + curated, **hash-pinned allowlist** (defense against poisoned models)
- ✅ GGUF resource sizer — given a model, computes required peer count + per-peer RAM (`/api/pools/request` sizes automatically)
- ✅ Pool orchestration: coordinator sizes a model, picks peers with enough RAM, instructs load, tracks WARMING→READY→DEGRADED
- ✅ **Volunteer nodes serving public inference** via peer-direct routing (gateway routes `/v1` to the peer serving the requested model)
- ✅ Reciprocity: signed-receipt contribution drives soft quota; Ed25519 signed-challenge auth (proof-of-key, not TOFU)
- ✅ Heartbeat into Redis-backed public registry (`/api/registry/nodes`)
- ✅ P2P mesh transport via Iroh (Ed25519 identity, no port-forward needed)
- ✅ Signed CBOR receipts per session (byte-counted, server-signed) aggregated into the gateway ledger
- ✅ Pool watchdog cron (alerts on FAILED/DEGRADED/under-quorum, silent when healthy)

**What does NOT work yet (roadmap):**
- ❌ Bandwidth limit enforcement (`bandwidth_mbps` is declared, not shaped)
- ❌ Partial-layer download (downloading only the layers a node will serve — still loads full GGUF per pool member)
- ❌ Trust/reputation with slashable commitments (receipts are signed and aggregated, but not yet economically bonded)
- ❌ mTLS between coordinator and peers (per-peer Cloudflare Tunnel auth is the current boundary)

The participant daemon **registers capacity**, **advertises active LLMs**, **serves public inference for allowlisted models**, and **earns reciprocity standing** through signed receipts. Larger models and partial-layer hosting remain on the roadmap below.

---

## Self-host the full stack

You need:
- 1 "master" Linux host (Debian/Ubuntu) — runs `llama-server` and the gateway
- 1+ "worker" Linux hosts (same architecture) — runs `rpc-server`
- A GGUF model (Qwen 2.5 1.5B Q4 is what the reference cluster uses; anything llama.cpp supports works)
- Optional: Cloudflare Tunnel for public HTTPS

```bash
# On each box (master + workers): compile llama.cpp
git clone https://github.com/maxwellmelo/prometeu.git
sudo bash prometeu/scripts/build-llama-cpp.sh

# On each worker:
sudo bash prometeu/scripts/install-worker.sh

# On the master:
# (Edit prometeu/gateway/config.example.json with your worker IPs)
sudo bash prometeu/scripts/install.sh
sudo cp /opt/models/your-model.gguf /opt/models/
sudo systemctl enable --now llama-server prometeu-gateway

# Test
curl http://localhost:3000/api/nodes
```

The gateway is a thin proxy — every endpoint llama-server exposes (chat, completions, embeddings, OpenAI-compatible) passes through.

### Prove the work is really distributed

```bash
bash scripts/prove-distribution.sh https://your-prometeu.example.com
```

Example output during a 120-token request:

```txt
node,cpu%,rx_mbps,tx_mbps,tcp,active,verdict
master,5.2,8.420,3.613,1,True,OK
worker1,10.0,2.236,4.509,1,True,OK
worker2,5.2,1.371,3.907,1,True,OK
```

If workers show CPU/network/TCP activity during the request, the forward pass is hitting those nodes.

---

## P2P mesh (experimental, in production)

`prometeu-mesh` is a small Rust binary using [Iroh](https://www.iroh.computer/) as the P2P transport. It replaces direct LAN connections with NAT-traversed encrypted streams.

```bash
# Worker: expose local llama.cpp rpc-server through the mesh
prometeu-mesh serve \
  --forward 127.0.0.1:50052 \
  --capability rpc-worker \
  --meta '{"model":"qwen2.5-1.5b","layers":"9-16","region":"home"}'

# Master: expose remote worker as a local TCP port
prometeu-mesh dial \
  --peer <worker-node-id-hex> \
  --listen 127.0.0.1:60052 \
  --capability rpc-worker
```

Properties:

- persistent Ed25519 identity; Iroh `NodeId` equals the public key
- no router port-forwarding required in normal NAT cases
- CBOR handshake (`DialerHello` / `ServerAck`)
- bidirectional TCP bridge over Iroh streams
- signed per-session receipts with byte counters (`prometeu/receipt/1`)
- Redis-backed discovery: `POST /api/mesh/announce`, `GET /api/mesh/peers`, `POST /api/mesh/leave`

The reference cluster has already cut production traffic over the mesh (master `llama-server` connects via local mesh dialer ports, not worker LAN IPs).

---

## Pools & model catalog

A client doesn't pick a worker — it requests a **model**, and the coordinator builds a pool:

```bash
# What models am I allowed to request? (curated, hash-pinned allowlist)
curl https://prometeu.mx3dev.com/api/catalog/allowlist

# Request a model be served; coordinator sizes it, picks peers, instructs load
curl -X POST https://prometeu.mx3dev.com/api/pools/request \
  -H 'content-type: application/json' \
  -d '{"model_id":"bartowski/Llama-3.2-1B-Instruct-GGUF/Llama-3.2-1B-Instruct-Q4_K_M.gguf","source":"hf","context":4096}'

# Watch the pool warm to quorum
curl https://prometeu.mx3dev.com/api/pools
```

- **Allowlist gate:** only curated models with a known sha256 can be loaded. Off-list or hash-mismatch → `403`. No fallback to "just trust the download" — the primary defense against poisoned weights.
- **Sizer:** GGUF metadata → required peer count + per-peer RAM. Importable as a lib or hit via the pool request.
- **State machine:** `REQUESTED → WARMING → READY`, with `DEGRADED`/`FAILED`/`STOPPED`. A `*/15min` watchdog alerts on incidents and stays silent when healthy.

---

## Reciprocity & auth

Public inference is governed by a **soft reciprocity quota**: serve to earn headroom, never hard-blocked.

- **Contribution** is measured from **signed receipts** (`tokens_served`), not uptime.
- **Consumption** is metered per `/v1` request.
- **Standing** = contributed / consumed → maps to a requests-per-minute soft cap. Anonymous callers get the floor; contributors get more.

Identity is **proof-of-key** (Ed25519), not trust-on-first-use:

```bash
# 1. get a one-time nonce for your public key
curl -X POST https://prometeu.mx3dev.com/api/auth/challenge \
  -H 'content-type: application/json' -d '{"public_key":"<base64-ed25519-pub>"}'

# 2. sign the nonce with your private key, then exchange for a bearer token
curl -X POST https://prometeu.mx3dev.com/api/auth/verify \
  -H 'content-type: application/json' \
  -d '{"public_key":"<pub>","nonce":"<nonce>","signature":"<base64-sig>"}'

# 3. check your standing (send the bearer token to be recognized as a contributor)
curl https://prometeu.mx3dev.com/api/reciprocity/standing \
  -H 'authorization: Bearer <token>'
```

The nonce is single-use, tokens are short-lived, and a bad signature is rejected (`401`) with no fallback to trusting the claimed key.

---

## Attribution requirements

**Prometeu is Apache 2.0 with an enforced attribution clause.** If you use any part of Prometeu — fork, embed, fine-tune, proxy, or build on top — you MUST comply with the [NOTICE](NOTICE) file. Highlights:

1. **UI attribution:** any user-facing interface (web, mobile, desktop, CLI) MUST display **"Powered by Prometeu"** as a visible hyperlink to https://github.com/maxwellmelo/prometeu. No `display:none`, no color-matched-to-background, no screen-reader-only.

2. **API header:** any HTTP API that wraps or proxies Prometeu MUST emit the header:
   ```
   X-Powered-By: Prometeu (https://github.com/maxwellmelo/prometeu)
   ```
   (The reference gateway emits this automatically — do not strip it.)

3. **Citations:** model cards, papers, blog posts, marketing materials referencing inference performed on a Prometeu node/cluster MUST cite the project by name with a link.

4. **NOTICE preservation:** the [NOTICE](NOTICE) file must travel with every redistribution.

Failure to comply terminates the Apache 2.0 license grant under §4(d). See full text in [NOTICE](NOTICE).

### Badge

Drop into your HTML/Markdown:

```markdown
[![Powered by Prometeu](https://img.shields.io/badge/powered_by-Prometeu-7c3aed?style=flat-square)](https://github.com/maxwellmelo/prometeu)
```

Or use the SVG in [`assets/powered-by-prometeu.svg`](assets/powered-by-prometeu.svg).

---

## Repository layout

```
gateway/      FastAPI proxy: routing, sizer, pools, reciprocity, allowlist, metrics, registry, mesh
mesh/         Rust binary (Iroh P2P transport, signed receipts)
node/         Participant daemon: installer, sandboxed inference runner, local dashboard
node-agent/   Lightweight telemetry agent (heartbeat capacity into registry)
web/          Minimal chat UI (HTML/CSS/JS, no build step)
scripts/      install + build scripts, systemd units, watchdog, proof tooling
tests/        pytest suite (router, sizer, pools, reciprocity, allowlist)
assets/       branding (SVG badge)
docs/         design notes
```

Run the test suite (42 tests):

```bash
cd prometeu && python3 -m venv .venv && . .venv/bin/activate
pip install -r gateway/requirements.txt pytest
pytest tests/ -q
```

---

## Roadmap

### ✅ Done
- 3-node distributed inference proof on 2011 CPU (no AVX2/BMI2)
- Per-node telemetry agent and proof script
- Public node registry (Redis TTL-based presence)
- Iroh P2P mesh: Ed25519 identity, discovery, TCP bridge, signed CBOR receipts
- Production cutover: master `llama-server` RPC routed over mesh dialer ports
- Real-time token accounting via `/v1/*` proxy (stream + non-stream)
- Per-IP rate limit (slowapi) on `/v1/*`
- Prometheus `/metrics` endpoint (path-label normalized to bound cardinality)
- Attribution header `X-Powered-By` on every gateway response
- Apache 2.0 license + NOTICE attribution clause
- Top-50 LLM catalog (HuggingFace GGUF + curated Ollama) selectable from node dashboard
- Public active-LLM stats: dashboard ranking active models by peer count + capacity
- Participant node GPU/VRAM detection (`nvidia-smi`, `rocm-smi`, DRM/sysfs)
- Enforced participant CPU/RAM limits via systemd cgroups (`CPUQuota`, `MemoryMax`)
- **Multi-target llama.cpp CI** (CUDA12 / aarch64 / Sandy-Bridge-safe x86_64 bundles)
- **Sandboxed inference runner** on participant nodes (dedicated user + systemd-run cgroups; refuses to serve if prereqs missing)
- **GGUF resource sizer** — model metadata → required peers + per-peer RAM (lib + `/api/pools/request`)
- **Pool orchestration** state machine (REQUESTED→WARMING→READY, DEGRADED/FAILED/STOPPED) with multi-pool support
- **Volunteer nodes serving public inference** via peer-direct routing
- **Reciprocity soft-quota** driven by signed-receipt contribution
- **Ed25519 signed-challenge auth** (proof-of-key, single-use nonce, short-lived bearer tokens)
- **Curated hash-pinned model allowlist** (poisoned-model defense, no-fallback verification)
- **Pool watchdog cron** (alerts on FAILED/DEGRADED/under-quorum, silent when healthy)
- Pool + reciprocity Prometheus metrics (`prometeu_pools_total`, `prometeu_reciprocity_*`)

### 🟡 In progress / planned
- Bandwidth enforcement for participant nodes (`bandwidth_mbps` traffic shaping)
- Grafana scraping `/metrics` → public transparency dashboard
- mTLS between coordinator and peers (beyond per-peer Cloudflare Tunnel auth)
- Economically-bonded / slashable receipt commitments (reputation layer)

### 🔬 Research (not committed)
- **Partial-layer download:** today llama.cpp loads the full GGUF on the master; workers receive tensors per-request. True per-peer layer hosting requires either (a) `gguf-split`-style sharding adapted for layer boundaries with a per-shard loader in llama.cpp, or (b) a [Petals](https://github.com/bigscience-workshop/petals)-style architecture using transformers directly. Needs a spike to validate feasibility before commitment.
- Heterogeneous workers (one CPU + one tiny GPU)
- Larger models (Qwen 7B Q4 = 4.4 GB; needs more RAM/nodes)
- Trust/reputation system for volunteer nodes (slashable signed-receipt commitments)

---

## License

[Apache License 2.0](LICENSE) with attribution clause in [NOTICE](NOTICE). See [Attribution requirements](#attribution-requirements) before redistributing.

## Credits

Built on the shoulders of [`llama.cpp`](https://github.com/ggerganov/llama.cpp) (MIT), [Qwen](https://qwenlm.github.io/) (Apache 2.0), [FastAPI](https://fastapi.tiangolo.com/) (MIT), [Iroh](https://www.iroh.computer/) (MIT/Apache), [slowapi](https://github.com/laurentS/slowapi) (MIT), and [Cloudflare Tunnel](https://www.cloudflare.com/products/tunnel/).

Reference cluster runs on Proxmox VE on a 2011 Lenovo ThinkPad in Fortaleza, Brazil.

---

## Prometeu (PT-BR)

> Inferência distribuída de LLM em hardware comum. Grátis, público, sem GPU.

**Prometeu** é um projeto Apache 2.0 pra rodar um único modelo de linguagem dividido em várias máquinas pequenas que, sozinhas, não conseguem hospedar o modelo. O cluster de referência roda `Qwen 2.5 1.5B Q4_K_M` dividido em três containers Linux sobre um Intel i7-2620M de **2011** — sem GPU, 8 GB RAM, sem AVX2, sem BMI2 — a ~9 tok/s na LAN e ~6.7 tok/s pela internet pública.

Não é rápido. Não é inovador. É a prova de que **dá pra rodar inferência distribuída em hardware que a maioria das pessoas jogaria fora** — exposto publicamente via Cloudflare Tunnel a custo zero.

Três coisas interessantes:

1. **Roda em hardware onde não devia.** CPUs Sandy Bridge não têm instruções que compiladores modernos adoram emitir em silêncio — a gente publica as flags de build que fixam isso.
2. **É dividido, não replicado.** Cada worker hospeda uma fatia do grafo de tensores. Nenhum worker tem o modelo inteiro na RAM.
3. **As peças são honestas.** Todo componente operacional é aberto: gateway FastAPI, units systemd, scripts de build, transporte P2P, protocolo de recibos assinados.

### Quero testar
- Web: https://prometeu.mx3dev.com
- API: `https://prometeu.mx3dev.com/v1/chat/completions` (compatível OpenAI)

### Quero hospedar um nó participante
```bash
sudo bash node/install.sh https://prometeu.mx3dev.com
```
Abre dashboard em `localhost:8787`. O instalador detecta CPU/GPU, fixa o binário llama.cpp certo e provisiona um runner de inferência **sandboxed** (usuário dedicado + cgroups). Com a allowlist hash-pinned, o nó **já serve inferência pública** dos modelos curados (roteamento peer-direct) e **acumula standing de reciprocidade** via recibos assinados. Falta ainda: shaping de banda, download por camada, reputação com bond. Detalhes em [Run a participating node](#run-a-participating-node).

### Quero deployar meu próprio cluster
Veja [Self-host the full stack](#self-host-the-full-stack) acima. Documentação técnica detalhada está em inglês.

### Atribuição obrigatória
Apache 2.0 + cláusula de "Powered by Prometeu" visível em qualquer interface derivada. Detalhes em [NOTICE](NOTICE). Não cumpriu = perdeu a licença.

Issues e PRs bem-vindos.
