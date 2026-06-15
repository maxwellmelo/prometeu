# Contributing

Issues and PRs welcome. Some directions that would be especially useful:

- **Make it easier to add workers.** Right now `/etc/systemd/system/llama-server.service` has the worker IPs hardcoded; a generator script would be nice.
- **Heterogeneous backends.** Mix a CPU node with a small GPU node and let llama.cpp distribute layers proportionally.
- **Public worker pool.** A trustless model where anyone can donate idle CPU cycles and earn... something. Open problem.
- **Better health UI.** The frontend already shows node badges; a per-layer breakdown would be educational.

For non-trivial changes please open an issue first so we can discuss the approach.

## Development

```bash
cd gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PROMETEU_LLAMA_URL=http://localhost:8080 \
PROMETEU_CONFIG=./config.example.json \
PROMETEU_WEB_DIR=../web \
uvicorn app:app --reload --port 3000
```

Frontend is pure HTML/CSS/JS — no build step. Edit `web/index.html` and reload.
