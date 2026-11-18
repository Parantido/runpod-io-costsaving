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
- SQLite-backed schedules with external-cron and in-app trigger modes.
- Hybrid deployment: host cron runs the local CLI; the same code runs in Docker.
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

State persists in `./data`: SQLite database (pods, schedules, jobs, logs),
generated secret, per-pod pod-ID files, and locks.

### Host and container ownership

Host cron and the container must write the same `./data` files with the same
UID/GID. Set them once:

```bash
export RUNPOD_OPS_UID=10001
export RUNPOD_OPS_GID=10001
sudo chown -R "$RUNPOD_OPS_UID:$RUNPOD_OPS_GID" data
```

Run host cron as that user (`runuser -u runpod -- ...`) or make the host user
match the container UID. Do not use `chmod 777`.

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
runpod-ops adopt riva <existing_pod_id>
runpod-ops schedules
runpod-ops schedule add riva-up --pod riva --action recreate \
  --cron "15 12 * * 1-6" --trigger external
runpod-ops schedule run riva-up
runpod-ops schedule enable riva-up
runpod-ops serve --host 0.0.0.0 --port 8080
```

Mutations stream engine output to stdout and exit non-zero on failure.

## Web UI

- Dashboard lists all configured pods with status and last job.
- Pod page shows policy, pod ID, and recent jobs.
- Action buttons queue a job; duplicate actions for the same pod return 409.
- Job page streams output and reports the exit code.
- Schedules page lists cron, timezone, trigger mode, next/last run, and jobs.
- Schedules can be created, edited, enabled, run now, or deleted. Hook
  commands stay in config/CLI and are not editable from the browser.
- JSON endpoints: `GET /api/pods`, `GET /api/pods/{name}`,
  `GET /api/health`.

## Schedules and host cron

Schedule definitions live in SQLite. `config.yaml` only holds pods and
settings.

Two trigger modes:

| Mode | Fires from | Host cron |
|------|------------|-----------|
| `external` | `runpod-ops schedule run <name>` | One cron line per schedule |
| `app` | scheduler thread in `runpod-ops serve` | Cron line must be removed |

Host cron uses local files, never `docker exec`:

```cron
15 12 * * 1-6 /root/mgmt/runpod/bin/runpod-ops-cron schedule run riva-02-up
11 4  * * *   /root/mgmt/runpod/bin/runpod-ops-cron schedule run riva-02-down
```

The wrapper sources `.env`, sets `RUNPOD_OPS_ROOT` and
`RUNPOD_OPS_DATA_DIR`, and execs the repository virtualenv CLI. Container and
host share the SQLite database through `./data`.

Migration flow:

1. Deploy the container alongside cron.
2. Create disabled SQLite schedules mirroring cron entries.
3. Change one cron line to `schedule run`; verify the job in the UI.
4. When ready, switch the schedule to `trigger=app`, enable it, remove the
   cron line.
5. Repeat per pod; keep `crontab.backup-*` files until all are migrated.

Hooks use `{root}` for path portability:

```json
["{root}/hooks/update-webservice.sh", "--pod-id-file", "{root}/data/pods/riva-02.pod_id"]
```

`{root}` is `/root/mgmt/runpod` on the host and `/app` in the container.

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
