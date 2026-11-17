# runpod-ops

Self-hosted control plane for RunPod GPU pods: lifecycle actions, GPU
failover, cost limits, alerts, job logs, and an authenticated web UI.

One deployment manages multiple named pods. Pod profiles live in YAML;
secrets come from the environment.

## Features

- Start, stop, recreate, and terminate named pods from CLI or browser.
- Automatic GPU selection with memory, vCPU, and price ceilings.
- Restart-or-recreate failover with backup rename and cleanup.
- Per-pod job queue and streaming logs stored in SQLite.
- Telegram and Loki notifications; optional SMTP job summaries.
- Single admin password with signed sessions and CSRF protection.
- Docker image, compose file, health check, and non-root runtime.
- Provider protocol ready for clouds other than RunPod.

## Quick Start

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# edit .env (ADMIN_PASSWORD, SECRET_KEY, RUNPOD_API_KEY, pod IDs)
# edit config.yaml (pod names, templates, GPU limits)
docker compose up -d --build
```

Open `http://localhost:8080`, sign in with `ADMIN_PASSWORD`, and manage pods.

State persists in `./data`: SQLite database, generated secret, and per-pod
pod-ID files.

## Configuration

Configuration loads from `--config`, then `RUNPOD_OPS_CONFIG`, then
`./config.yaml`, then `/data/config.yaml`. Environment variables override
YAML values. YAML strings support `${VAR}` expansion.

Environment variables:

| Variable | Purpose |
|----------|---------|
| `ADMIN_PASSWORD` | Web login password (required) |
| `SECRET_KEY` | Cookie/CSRF signing key; generated under `data/` if absent |
| `RUNPOD_API_KEY` | RunPod API key |
| `RUNPOD_API_KEY_FILE` | Alternative path to a key file |
| `RUNPOD_OPS_DATA_DIR` | State directory (default `/data`) |
| `RUNPOD_OPS_HOST`, `RUNPOD_OPS_PORT` | Bind address and port |
| `RUNPOD_OPS_SECURE_COOKIES` | Set `true` behind HTTPS |
| `TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts |
| `LOKI_ENABLED`, `LOKI_URL` | Loki log shipping |
| `SMTP_ENABLED`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO` | Email job summaries |

Pod profile fields:

| Field | Meaning |
|-------|---------|
| `name` | Unique name; letters, digits, dot, dash, underscore |
| `template_id` | RunPod template (required) |
| `network_volume_id` | Optional network volume |
| `image_name` | Optional container image override |
| `preferred_gpu` | GPU tried first when recreating |
| `fallback_name` | Name for recreated pods; defaults to `name` |
| `min_gpu_mem_gb`, `min_vcpu`, `max_price` | GPU eligibility |
| `search_timeout`, `poll_interval` | Failover polling limits |

See `config.example.yaml` for a complete example.

## CLI

```bash
runpod-ops --config config.yaml pods
runpod-ops status riva
runpod-ops start riva
runpod-ops stop riva
runpod-ops recreate riva
runpod-ops terminate riva
runpod-ops gpus --min-mem 80 --max-price 4
runpod-ops jobs --pod riva
runpod-ops serve --host 0.0.0.0 --port 8080
```

Mutations stream engine output to stdout and exit non-zero on failure.

## Web UI

- Dashboard lists all configured pods with status and last job.
- Pod page shows policy, pod ID, and recent jobs.
- Action buttons queue a job; duplicate actions for the same pod return 409.
- Job page streams output and reports the exit code.
- JSON endpoints: `GET /api/pods`, `GET /api/pods/{name}`,
  `GET /api/health`.

## Security

- Run behind a TLS-terminating reverse proxy and set
  `RUNPOD_OPS_SECURE_COOKIES=true`.
- Keep `.env`, `config.yaml`, and `data/` out of version control.
- Secrets are read only from environment variables or key files.
- Job commands are stored with token and password values masked.
- The container runs as a non-root user.
- Only one admin account exists; v1 is not multi-tenant.

See [SECURITY.md](SECURITY.md) for reporting guidance.

## Local Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

Run the UI locally:

```bash
ADMIN_PASSWORD=pw RUNPOD_OPS_DATA_DIR=./data \
  .venv/bin/runpod-ops --config config.yaml serve
```

## Architecture

```text
runpod_ops/
  engine.py      tested RunPod GraphQL/failover engine (preserved)
  config.py      YAML + env configuration
  state.py       SQLite pods, jobs, logs, audit
  provider.py    PodProvider protocol and RunPod adapter
  service.py     profile-to-engine command construction, pod-ID sync
  jobs.py        in-process worker with per-pod locking
  auth.py        password, sessions, CSRF
  cli.py         runpod-ops command line
  web/           FastAPI routes, templates, static assets
```

Mutations execute the engine CLI in a subprocess so long failover loops are
isolated and their output can stream to the browser. Read-only status uses
the engine provider directly. The engine keeps its original module path via a
compatibility shim at `runpod_costsaving.py`.

The original single-tenant shell scripts, LLM lifecycle, and external DNS
integration moved to `legacy/` and are not part of v1.

## Tests

```bash
.venv/bin/pytest -q
```

## License

MIT. See [LICENSE](LICENSE).
