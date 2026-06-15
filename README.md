# Prometeu

> Distributed LLM inference on three 2011-era home servers. Free, public, no GPU.

🔗 **Live demo:** [prometeu.mx3dev.com](https://prometeu.mx3dev.com) — talk to a small open LLM running on hardware so old it can't even run modern compilers cleanly.

🇧🇷 [Versão em português](#prometeu-pt-br)

---

## What is this

**Prometeu** runs a single language model split across three Linux containers, each on the same physical box — an Intel i7-2620M from **2011**, no GPU, 8 GB RAM, no AVX2, no BMI2.

It uses [`llama.cpp`](https://github.com/ggerganov/llama.cpp)'s built-in **RPC backend**: one node hosts `llama-server` and shards the model's tensors over TCP to two workers running `rpc-server`. End-to-end, three nodes cooperate to produce ~9 tokens/sec on `Qwen 2.5 1.5B Q4_K_M`.

This isn't fast. It isn't groundbreaking. It's a proof that **you can run distributed LLM inference on hardware most people would throw away** — and that, with a little discipline, you can put it on the public internet for free.

## Stack

```
[user] ──TLS──▶ Cloudflare Tunnel ──▶ FastAPI gateway :3000
                                          │
                                          ▼ proxy /v1/*
                                      llama-server :8080  (master)
                                          │
                                          ▼ RPC tensors
                                rpc-server :50052 (worker 1)
                                rpc-server :50052 (worker 2)
```

| Component | Role |
|---|---|
| `llama-server` (master) | OpenAI-compatible HTTP API, loads model, orchestrates RPC |
| `rpc-server` (workers) | Hosts a slice of the model's tensor graph, computes on demand |
| FastAPI gateway | Thin proxy + cluster telemetry (`/api/nodes`) |
| Node Agent | Per-node CPU/RAM/network/process telemetry on `:9100` |
| HTML/JS frontend | Minimal chat UI with SSE streaming and live node badges |
| Cloudflare Tunnel | Public HTTPS without opening firewall ports |

## Performance, honestly

| Setup | Tokens/sec |
|---|---|
| 3-node split, LAN | ~9.18 |
| 3-node split, over public internet (https) | ~6.7 |
| First token latency, warm cache | ~0.5s |
| First token latency, cold (post-reboot) | 30–90s |

For comparison: a single modern GPU does ~100 tok/s on the same model. We're not competing on speed — we're competing on **dollars spent**.

## The interesting technical bit

The CPU is **Sandy Bridge** (2011). It has AVX but no AVX2, no BMI2, no FMA, no F16C.

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

The `cmake` flags pick the right ggml kernels; the `-mno-*` flags stop the compiler from sneaking unsupported instructions into the binary anyway. Pinned at `-march=sandybridge`, the binary runs on any Sandy/Ivy Bridge box.

Full script: [`scripts/build-llama-cpp.sh`](scripts/build-llama-cpp.sh).

## Install it on your own boxes

You need:
- 1 "master" Linux host (Debian/Ubuntu) — runs `llama-server` and the gateway
- 1+ "worker" Linux hosts (same architecture) — runs `rpc-server`
- A GGUF model (Qwen 2.5 1.5B Q4 is what I use; anything llama.cpp supports works)
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

## Prove the work is really distributed

The live dashboard shows per-node CPU/RAM/network and TCP connection counts.
For a CLI proof, run:

```bash
bash scripts/prove-distribution.sh https://prometeu.mx3dev.com
```

Example result during a 120-token request:

```txt
node,cpu%,rx_mbps,tx_mbps,tcp,active,verdict
master,5.2,8.420,3.613,1,True,OK
worker1,10.0,2.236,4.509,1,True,OK
worker2,5.2,1.371,3.907,1,True,OK
```

If workers show CPU/network/TCP activity while the request is running, the forward pass is hitting those nodes.

## OpenAI-compatible API

Drop-in for the OpenAI Python SDK:

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

## Repository layout

```
gateway/      FastAPI proxy + node telemetry
web/          minimal chat UI (HTML/CSS/JS, no build step)
scripts/      install + build scripts, systemd units
docs/         design notes
```

## Public worker pool (experimental)

Sprint 2A adds a participant-facing daemon:

```bash
sudo bash node/install.sh https://prometeu.mx3dev.com
```

It opens a local dashboard on `http://localhost:8787`, detects hardware, lets the participant choose CPU/RAM/bandwidth/model limits, and heartbeats into the coordinator registry:

```txt
POST /api/registry/join
POST /api/registry/heartbeat
GET  /api/registry/nodes
POST /api/registry/leave
```

Important: this phase only registers capacity. It does **not** route public inference to volunteer nodes yet. Public serving needs an overlay network (WireGuard-style) plus layer assignment and trust/reputation.

## Roadmap

- [x] Fixed 3-node distributed inference proof
- [x] Per-node telemetry agent and proof script
- [x] Public node registry + local node dashboard
- [ ] WireGuard overlay so nodes can join without opening ports
- [ ] Coordinator layer scheduler / auto-split
- [ ] Per-IP rate limit (slowapi)
- [ ] Heterogeneous workers (one CPU + one tiny GPU)
- [ ] Larger models (Qwen 7B Q4 = 4.4 GB; needs more RAM/nodes)

## License

MIT.

## Credits

Built on the shoulders of [`llama.cpp`](https://github.com/ggerganov/llama.cpp), [Qwen](https://qwenlm.github.io/), [FastAPI](https://fastapi.tiangolo.com/), and [Cloudflare Tunnel](https://www.cloudflare.com/products/tunnel/).

---

## Prometeu (PT-BR)

> Inferência distribuída de LLM em três servidores caseiros de 2011. Grátis, público, sem GPU.

**Prometeu** roda um único modelo de linguagem dividido em três containers Linux, todos na mesma máquina física — um Intel i7-2620M de 2011, sem GPU, 8 GB de RAM, sem AVX2, sem BMI2.

Usa o backend **RPC** do `llama.cpp`: um nó hospeda o `llama-server` e distribui os tensores do modelo por TCP para dois workers rodando `rpc-server`. No fim, os três nós cooperam pra produzir ~9 tokens/segundo no `Qwen 2.5 1.5B Q4_K_M`.

Não é rápido. Não é inovador. É a prova de que **dá pra rodar inferência distribuída de LLM em hardware que a maioria das pessoas jogaria fora** — e, com um pouco de disciplina, colocar isso na internet pública de graça.

Aperta no [link da demo](https://prometeu.mx3dev.com) e testa.

Documentação técnica detalhada está acima, em inglês. O fix que faz a stack rodar em CPU Sandy Bridge sem BMI2 está em [`scripts/build-llama-cpp.sh`](scripts/build-llama-cpp.sh) — se você já bateu cabeça com `SIGILL` ao compilar llama.cpp em CPU antiga, essa é a fórmula.

Issues e PRs bem-vindos.
