# Architecture

## High-level

```
                       ┌──────────────────────────────────┐
                       │   Public Internet                │
                       └────────────┬─────────────────────┘
                                    │ HTTPS
                                    ▼
                       ┌──────────────────────────────────┐
                       │   Cloudflare Edge                │
                       │   - TLS termination              │
                       │   - DDoS / WAF                   │
                       │   - Bot blocking (robots.txt)    │
                       └────────────┬─────────────────────┘
                                    │ cloudflared tunnel
                                    ▼
                       ┌──────────────────────────────────┐
                       │   Prometeu Master node           │
                       │   ─────────────────────────────  │
                       │   :3000  FastAPI gateway         │
                       │           - /         (frontend) │
                       │           - /api/nodes           │
                       │           - /api/health          │
                       │           - /v1/*  (proxy)       │
                       │                                  │
                       │   :8080  llama-server (master)   │
                       │           - holds model weights  │
                       │           - tensor split via RPC │
                       └────────────┬─────────────────────┘
                                    │ TCP RPC
                  ┌─────────────────┼─────────────────┐
                  ▼                                   ▼
       ┌──────────────────┐                ┌──────────────────┐
       │ Worker 1         │                │ Worker 2         │
       │ rpc-server :50052│                │ rpc-server :50052│
       │ layers 9–18      │                │ layers 19–27     │
       └──────────────────┘                └──────────────────┘
```

## Why RPC?

`llama.cpp`'s RPC backend ships a slice of the ggml computation graph from a
master process to one or more `rpc-server` processes. The master keeps
control (sampling, tokenization, KV cache), the workers do the matrix
multiplications for the layers assigned to them.

- **Pros:** Linear-ish memory split, OpenAI-compatible API at the edge, no
  custom client code.
- **Cons:** Synchronous (any worker stall stalls the whole forward pass),
  network-sensitive (workers should be on the same LAN), single point of
  failure (master).

## Health model

Telemetry on `/api/nodes` deliberately reports cluster-level health rather
than probing each worker individually. The rpc-server backlog defaults to 1
connection, and during active inference new TCP connects get queued or
timeout — making naive probes unreliable.

Instead the gateway polls `llama-server`'s `/health` every 5s. RPC is
synchronous: if `llama-server` says `ok`, every worker is necessarily
participating. If any worker dies, `/health` immediately starts failing.

## Cold start

First request after a reboot is slow (30–90s) because `llama-server` only
streams tensor slices to workers on-demand at startup. After that, every
subsequent request reuses the loaded weights.

## Public exposure

There is no public attack surface other than the Cloudflare Tunnel. The
master's `llama-server` binds to `127.0.0.1:8080` and the workers bind to
`10.x.y.z:50052` on a private LAN. The only thing exposed to the internet
is the FastAPI gateway, reached through a Cloudflare-managed outbound
tunnel.

## Failure modes worth knowing

- **Worker reboot mid-request:** Active request fails. Next request loads
  the model again (cold start).
- **Master OOM:** Reduce `-c` (context size) — KV cache grows linearly.
- **Worker network blip:** llama-server times out the forward pass, returns
  an error. Cluster recovers as soon as TCP reconnects.
