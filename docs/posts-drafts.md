# Posts — Marco 1 Prometeu

Drafts para publicação após repo GitHub estar live.

---

## 1. Cultura Builder (PT-BR) — categoria `geral` ou `projetos`

**Limites:** 1200 chars, 1 post/dia, sem edit via MCP.

```
Postei um LLM rodando em hardware de 2011 ao vivo na internet.

prometeu.mx3dev.com — Qwen 2.5 1.5B respondendo perguntas, gratuito, sem login.

A graça não é o modelo. É a infra:

- 1 notebook Intel i7-2620M (2011, sem GPU, sem AVX2, sem BMI2)
- 3 containers LXC no Proxmox compartilhando 1 modelo via llama.cpp RPC
- FastAPI proxy + chat HTML puro + Cloudflare Tunnel
- Custo mensal: R$ 0 (energia já pago)

Performance honesta:
- ~9 tok/s LAN, ~6.7 tok/s público
- Primeira resposta cold: 30-90s; depois 1-3s

O bug mais cabeludo: GCC moderno emite `shlx` (BMI2) mesmo com `-DGGML_AVX2=OFF`. Crash SIGILL imediato em Sandy Bridge. Fix exige `-mno-bmi2 -march=sandybridge` explícito + flag GGML.

Repo aberto: github.com/maxwellmelo/prometeu (MIT)
README tem o fix completo + scripts de install.

Próximo passo é pool voluntário (qualquer um doa CPU idle pra rodar inferência distribuída) — ainda em desenho. Aceito ideias.
```

Char count: ~1090.

---

## 2. Show HN (EN)

**Title:** `Show HN: Prometeu – distributed LLM inference on three 2011 home servers`

**URL:** `https://prometeu.mx3dev.com`

**First comment:**

```
Hi HN — I'm Maxwell from Fortaleza, Brazil.

Prometeu is a small experiment: can you run distributed LLM inference on
hardware that's old enough to drive? Turns out yes.

Stack:
- 1 Intel i7-2620M from 2011 (no GPU, no AVX2, no BMI2, 8GB RAM)
- 3 LXC containers on Proxmox, splitting one Qwen 2.5 1.5B model
- llama.cpp's RPC backend wires them together over TCP
- FastAPI gateway exposes an OpenAI-compatible API
- Cloudflare Tunnel for free public HTTPS

Performance is what you'd expect: ~9 tok/s on the LAN, ~6.7 over the public
internet. Cold start after reboot takes 30-90s as the model streams to the
workers via RPC.

The fun technical bit: compiling llama.cpp on Sandy Bridge is a minefield.
GCC happily emits `shlx` (BMI2) instructions even when you ask CMake to
disable AVX2. The binary builds, runs once, and dies with SIGILL. The fix
is a belt-and-suspenders compile invocation that pins -march=sandybridge
and sets -mno-bmi2 explicitly. Full script in the repo.

Code (MIT): https://github.com/maxwellmelo/prometeu
Demo: https://prometeu.mx3dev.com

What I'm exploring next: a voluntary public worker pool where anyone with
idle CPU cycles can join the cluster. Suggestions welcome.
```

---

## 3. Reddit — r/LocalLLaMA

**Title:** `I split a single Qwen 1.5B across 3 LXC containers on a 2011 laptop via llama.cpp RPC. 9 tok/s, public demo.`

```
Built this over the weekend with R$ 0 budget.

Hardware: one Intel i7-2620M (no GPU, no AVX2, no BMI2, 8GB RAM). One
physical box.

Setup: three Debian LXC containers on Proxmox. Master runs llama-server
holding the model weights and orchestrating RPC. Workers run rpc-server
and host a slice of the tensor graph each.

Numbers:
- Qwen 2.5 1.5B Q4_K_M
- 9.18 tok/s on the LAN, 6.7 tok/s through Cloudflare Tunnel
- Cold start: 30-90s; warm: 1-3s per response
- KV cache fits comfortably with -c 1024

The compile chain on Sandy Bridge was the actual hard part. llama.cpp's
CMake disables AVX2 cleanly, but GCC will still emit BMI2 instructions
(shlx specifically) unless you set -mno-bmi2 explicitly. SIGILL on first
forward pass. Build script in the repo has the exact flags.

Live: https://prometeu.mx3dev.com
Code: https://github.com/maxwellmelo/prometeu (MIT)

Curious to hear from anyone running similar splits on heterogeneous nodes —
e.g. one CPU + one tiny GPU worker.
```

---

## 4. Reddit — r/selfhosted (opcional)

**Title:** `Public chat LLM on a 2011 laptop with three LXC containers — 9 tok/s, free Cloudflare Tunnel`

```
Same project as posted on r/LocalLLaMA but pitched for the selfhosted crowd.

The selfhost angle:
- One physical box, three LXC containers on Proxmox
- llama.cpp RPC for tensor split (no Docker, no GPU)
- FastAPI gateway with OpenAI-compatible API
- Cloudflare Tunnel for public HTTPS without port forwarding
- systemd units for everything; survives reboots
- Cost: $0/month above existing electricity

Stack is in github.com/maxwellmelo/prometeu (MIT). Live at
prometeu.mx3dev.com.

If you've been wondering whether your old laptop can do something useful as
a chat LLM — yes, with a few caveats about cold start latency.
```
```
