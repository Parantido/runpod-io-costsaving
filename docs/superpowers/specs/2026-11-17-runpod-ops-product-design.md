# RunPod Ops Product Design

## Goal

Turn the single-tenant RunPod cost-saving scripts into a self-hosted,
Dockerized operations product that another operator can deploy, configure,
and control through both a CLI and a web UI.

## Users and Use Cases

- A self-hosting operator runs one container with one admin password.
- The operator declares one or more named RunPod pods in YAML.
- From CLI or web UI they can list pods, inspect status, start, stop,
  restart-or-recreate, and view operation logs.
- Failover stays automatic: when restart is impossible the engine searches
  for a qualifying GPU, recreates the pod, and cleans up the backup.

## v1 Scope

In scope:

- Named multi-pod management from one deployment.
- Pod lifecycle: start, stop, restart-or-recreate, terminate.
- Existing GPU selection, failover, backup/rename, and retry behavior.
- Telegram and Loki alerts and logs, configured per deployment.
- Optional SMTP job-result email.
- SQLite state for pod IDs, jobs, logs, and audit events.
- CLI `runpod-ops`.
- Web UI with authenticated full control.
- Docker image and compose file.
- Config via YAML plus environment variable overrides.
- Tests, CI config, and operator documentation.

Out of scope for v1:

- Multi-user accounts or per-user RunPod keys.
- Hosted SaaS operation.
- Additional cloud providers. A provider protocol is defined so adapters
  can be added later.
- Cron-like schedules in the UI.
- Hetzner DNS updates and the external webservice updater.
- Rewriting the tested failover engine.

## Architecture

```text
runpod_ops/
  config.py          YAML + env configuration, pod profiles
  state.py           SQLite state and query helpers
  provider.py        Provider protocol + RunPod adapter
  engine.py          Existing tested RunPod engine (moved unchanged)
  service.py         Builds engine CLI commands and syncs pod state
  jobs.py            In-process job worker with per-pod lock
  notifiers.py       Optional SMTP job-result notifications
  auth.py            Admin password, signed sessions, CSRF
  cli.py             `runpod-ops` command line
  web/
    app.py           FastAPI application factory
    routes.py        HTML and JSON routes
    templates/       Jinja2 templates
    static/          CSS
tests/
  test_config.py
  test_state.py
  test_jobs.py
  test_auth.py
  test_web.py
  test_runpod_costsaving.py   (existing, migrated)
  test_llm_lifecycle.py       (existing, retained only if kept)
```

The existing `runpod_costsaving.py` is moved to `runpod_ops/engine.py`.
A root-level compatibility shim re-exports it so existing imports and tests
continue to work. The engine keeps ownership of GraphQL queries, GPU scoring,
failover, rename/recreate, and alert delivery. New code adapts it rather than
reimplementing it.

Mutations run the engine CLI in a subprocess so the original orchestration
is preserved and its output can be streamed to the job log. Read-only status
calls use the engine's `CloudManager` / `RunPodManager` classes directly.
Process boundaries are deliberate: they isolate long-running failover loops
and preserve the already-tested control flow.

## Configuration

Load order: `--config` path, then `RUNPOD_OPS_CONFIG`, then
`./config.yaml`, then `/data/config.yaml`. Environment variables override
YAML. YAML supports `${VAR}` expansion and never stores secrets directly.

```yaml
data_dir: /data
server:
  host: 0.0.0.0
  port: 8080
runpod:
  api_key_file: ${RUNPOD_API_KEY_FILE}
notifications:
  telegram:
    enabled: true
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
  loki:
    enabled: true
    url: ${LOKI_URL}
  smtp:
    enabled: false
    host: ${SMTP_HOST}
    port: 587
    username: ${SMTP_USERNAME}
    password: ${SMTP_PASSWORD}
    from: ${SMTP_FROM}
    to: ${SMTP_TO}
defaults:
  min_gpu_mem_gb: 80
  min_vcpu: 16
  max_price: 4.00
  search_timeout: 1200
  poll_interval: 15
pods:
  - name: riva
    template_id: ${RIVA_TEMPLATE_ID}
    network_volume_id: ${RIVA_NETWORK_VOLUME_ID}
    image_name: docker.io/example/riva:latest
    preferred_gpu: NVIDIA H200
    min_gpu_mem_gb: 140
    min_vcpu: 20
    max_price: 4.00
```

Required per pod: unique `name`, `template_id`. An existing pod ID is
discovered from SQLite or the per-pod state file. Missing pod ID plus no
template means the action fails with a clear error.

Admin authentication uses `ADMIN_PASSWORD` and `SECRET_KEY` (a generated
secret persisted to the data directory if absent). API keys, tokens, and
passwords appear only in environment variables, never in YAML, logs, or the
image.

## State Model

SQLite at `${data_dir}/runpod_ops.db`:

- `pods(name TEXT PRIMARY KEY, pod_id TEXT, status TEXT, updated_at TEXT)`
- `jobs(id TEXT PRIMARY KEY, pod TEXT, action TEXT, status TEXT,
  exit_code INTEGER, created_at TEXT, started_at TEXT, finished_at TEXT,
  command TEXT)`
- `job_logs(job_id TEXT, seq INTEGER, ts TEXT, line TEXT)`
- `audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT,
  pod TEXT, detail TEXT)`

The engine still uses a per-pod pod-id file under `${data_dir}/pods/`.
After each mutation the service reads that file and synchronizes SQLite.
Job commands are stored with tokens and passwords masked.

## Job Model

- One worker thread started with the application.
- One queued job per pod at a time; repeated actions for the same pod are
  rejected with HTTP 409.
- Job statuses: `queued`, `running`, `succeeded`, `failed`.
- Output is appended line by line and served to the UI by polling.
- On failure, the exit code and last log lines are surfaced, and the
  optional SMTP notifier sends a summary.
- The worker and job locks are in-process. Uvicorn runs with one worker.

## Web UI

- `GET /login`, `POST /login`, `POST /logout`.
- `GET /` dashboard: pods, status, last job, action buttons.
- `GET /pods/{name}`: pod detail, GPU candidates, recent jobs.
- `POST /pods/{name}/start|stop|recreate|terminate`: queue a job.
- `GET /jobs/{id}`: job detail with log polling fragment.
- `GET /jobs/{id}/log`: HTML fragment of log lines.
- `GET /api/health`: unauthenticated health check.
- `GET /api/pods`: authenticated JSON pod list.
- `GET /api/pods/{name}`: authenticated JSON pod status.

Auth: `ADMIN_PASSWORD` checked in constant time; signed, expiring session
cookie; CSRF token required on mutating requests. All state-changing routes
require authentication. Secrets never rendered in HTML.

## CLI

```bash
runpod-ops serve
runpod-ops pods
runpod-ops status <name>
runpod-ops start <name>
runpod-ops stop <name>
runpod-ops recreate <name>
runpod-ops terminate <name>
runpod-ops gpus [--min-mem N] [--max-price N]
runpod-ops jobs [--pod NAME]
```

Mutation commands stream engine output to stdout and exit non-zero on
failure. `serve` starts uvicorn.

## Provider Expansion Point

`provider.py` defines a `PodProvider` protocol with read operations
(`get_pod`, `list_gpus`) and command construction for mutations. v1 ships
`RunPodProvider` only. A future provider adds an adapter without changing
web or job code.

## Deployment

- Multi-stage Dockerfile, Python 3.12 slim, non-root user, `runpodctl`
  installed for start/stop, health check on `/api/health`.
- `compose.yaml` mounts `./data:/data`, exposes `8080`, reads `.env`.
- Config and `.env` examples included; secrets stay outside the image.
- `/data` holds SQLite plus per-pod state files.

## Security and Operations

- Never commit `.env`, `config.yaml`, API keys, pod state, or logs.
- Redact token/password arguments before storing job commands.
- Constant-time password comparison and signed session cookies.
- Per-pod lock prevents concurrent destructive actions.
- Audit every mutation with actor and action.
- Engine subprocess receives only the args needed for the selected pod.
- README documents reverse-proxy TLS termination and secret handling.

## Migration Plan

1. Move the engine into the package with a compatibility shim; run existing
   tests unchanged.
2. Add environment-only API key support to the engine.
3. Add config, state, provider, service, and job modules with tests.
4. Add CLI and web UI with tests.
5. Add Docker, compose, docs, CI, and examples.
6. Remove or archive v1-excluded scripts from the product tree; keep them
   under `legacy/` until users migrate.

## Testing Strategy

- Unit tests for config precedence and redaction.
- Unit tests for SQLite state transitions and job locking.
- Auth tests for password check, session signature, expiry, and CSRF.
- Web tests with a fake provider and fake engine command, asserting
  authentication, action queuing, and 409 on concurrent pod actions.
- Existing engine tests remain green through the compatibility shim.
- Manual smoke test: `docker compose up`, login, list, queue a dry-run
  action against a fake pod, inspect streamed logs.

## Success Criteria

- A new operator can deploy with Docker, provide env secrets, list pods,
  and run lifecycle actions from the browser.
- Multiple named pods are managed independently.
- Existing failover behavior and alerts still work through the CLI.
- No secrets or tenant-specific identifiers remain in the repository.
- Tests and CI pass for the new modules and the preserved engine.
