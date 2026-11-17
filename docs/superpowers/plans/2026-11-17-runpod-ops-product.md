# RunPod Ops Product Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the single-tenant RunPod management scripts into a self-hosted Dockerized product with multi-pod YAML config, SQLite state, a CLI, and an authenticated web control UI.

**Architecture:** Preserve the tested `runpod_costsaving.py` engine by moving it into `runpod_ops/engine.py` behind a compatibility shim. New modules wrap the engine: config, state, provider, service, jobs, auth, CLI, and FastAPI web UI. Mutations run the engine CLI in a subprocess, streaming output into SQLite job logs; read-only calls use engine classes directly.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, Jinja2 + HTMX, PyYAML, stdlib sqlite3, pytest, Docker.

**Spec:** `docs/superpowers/specs/2026-11-17-runpod-ops-product-design.md`

## Global Constraints

- Python floor: 3.12.
- Keep the existing engine behavior; do not rewrite failover logic.
- No secrets in YAML, git, logs, image, or rendered HTML.
- Config secrets come from environment variables only.
- Uvicorn runs with one worker; job locking is in-process.
- All state-changing web routes require auth and CSRF.
- Commands and tests use `.venv/bin/python` / `.venv/bin/pytest`.
- Do not commit unless the operator explicitly asks.
- v1 is RunPod-only; provide a provider protocol for future adapters.

---

### Task 1: Preserve the engine and create package scaffolding

**Files:**
- Create: `runpod_ops/__init__.py`
- Create: `runpod_costsaving.py` (compatibility shim)
- Move: `runpod_costsaving.py` -> `runpod_ops/engine.py`
- Create: `pyproject.toml`
- Modify: `.gitignore`
- Test: `tests/test_runpod_costsaving.py` (unchanged)

**Interfaces:**
- Produces: `runpod_ops.engine` containing all existing classes/functions.
- Produces: root `runpod_costsaving` shim exporting engine symbols.
- Produces: package installable with `pip install -e .`; console script `runpod-ops = runpod_ops.cli:main` (module added later).

- [ ] **Step 1: Move engine unchanged**

Run: `git mv runpod_costsaving.py runpod_ops/engine.py` after creating the directory. Create `runpod_ops/__init__.py` with a version string.

- [ ] **Step 2: Add compatibility shim**

```python
from runpod_ops.engine import *  # noqa: F401,F403
```

- [ ] **Step 3: Add pyproject and ignore dev artifacts**

`pyproject.toml` declares Python >=3.12, dependencies fastapi, uvicorn, jinja2, pyyaml, optional dev pytest/httpx, and console script. Add `.venv/` and `.pytest_cache/` to `.gitignore` if absent.

- [ ] **Step 4: Verify existing tests**

Run: `.venv/bin/pytest tests/test_runpod_costsaving.py -q`
Expected: all existing tests pass.

---

### Task 2: Environment-driven engine configuration

**Files:**
- Modify: `runpod_ops/engine.py:27-45` (`read_config_value`)
- Modify: `runpod_ops/engine.py:834-862` (`CloudManager` API key)
- Test: `tests/test_engine_env.py`

**Interfaces:**
- Produces: `read_config_value(key, default)` checks `os.environ[key]` first, then config file, then default.
- Produces: `CloudManager._get_api_key()` checks `RUNPOD_API_KEY`, then `RUNPOD_API_KEY_FILE`, then legacy path.

- [ ] **Step 1: Write failing tests**

```python
def test_read_config_value_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("FOO_SETTING", "env")
    assert read_config_value("FOO_SETTING", "default") == "env"

def test_api_key_prefers_env(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "secret")
    assert CloudManager()._get_api_key() == "secret"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/test_engine_env.py -q`
Expected: FAIL because env is ignored.

- [ ] **Step 3: Implement env precedence**

Use `os.environ.get(key)` before reading `config.sh`. In `_get_api_key`, check `RUNPOD_API_KEY`, then `RUNPOD_API_KEY_FILE`, then the legacy constant. Never log the key.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_engine_env.py tests/test_runpod_costsaving.py -q`
Expected: PASS.

---

### Task 3: Configuration loader

**Files:**
- Create: `runpod_ops/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces dataclasses `ServerConfig`, `RunPodConfig`, `NotificationConfig`, `SMTPConfig`, `PodProfile`, `AppConfig`.
- Produces `load_config(path: str | None = None, environ: dict | None = None) -> AppConfig`.
- Produces `AppConfig.get_pod(name) -> PodProfile` raising `KeyError`.
- `load_config` precedence: explicit path, `RUNPOD_OPS_CONFIG`, `./config.yaml`, `/data/config.yaml`; missing file with no explicit path yields defaults for server but requires pods later.
- Expands `${VAR}` in strings; missing variables become empty strings.
- Env overrides: `RUNPOD_OPS_DATA_DIR`, `RUNPOD_OPS_HOST`, `RUNPOD_OPS_PORT`, `ADMIN_PASSWORD`, `SECRET_KEY`, notifier and RunPod key vars.
- Validation: unique non-empty pod names; non-empty `template_id`; positive numeric limits.

- [ ] **Step 1: Write failing tests**

Test YAML loading, `${VAR}` expansion, env override precedence, duplicate pod rejection, missing template rejection, and `get_pod`.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: FAIL because module is missing.

- [ ] **Step 3: Implement minimal loader**

Use `yaml.safe_load`, recursive `${VAR}` substitution, dataclass construction with defaults, and explicit `ConfigError` for validation failures. Do not put secrets in `repr`.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: PASS.

---

### Task 4: SQLite state

**Files:**
- Create: `runpod_ops/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces class `StateStore(db_path: str | Path)` with `init_schema()`, `upsert_pod(name, pod_id, status)`, `get_pod(name)`, `list_pods()`, `create_job(pod, action, command) -> str`, `start_job(job_id)`, `finish_job(job_id, status, exit_code)`, `add_log(job_id, line)`, `get_job(job_id)`, `list_jobs(pod=None, limit=50)`, `get_logs(job_id, after_seq=0)`, `audit(actor, action, pod, detail)`.
- Job IDs are `uuid4().hex`. Timestamps are UTC ISO-8601.
- A connection is opened per operation; `sqlite3` row factory returns dict-like rows.

- [ ] **Step 1: Write failing tests**

Test schema creation, pod upsert/read, job lifecycle, log ordering and `after_seq`, listing filters, and audit insertion.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/test_state.py -q`
Expected: FAIL because module is missing.

- [ ] **Step 3: Implement state store**

Use parameterized SQL only. Enable `PRAGMA journal_mode=WAL`. Create schema idempotently. Store timestamps with `datetime.now(timezone.utc).isoformat()`.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_state.py -q`
Expected: PASS.

---

### Task 5: Authentication helpers

**Files:**
- Create: `runpod_ops/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces `AuthManager(password: str, secret: str, ttl_seconds: int = 28800)`.
- `check_password(candidate: str) -> bool` uses `hmac.compare_digest`.
- `create_session() -> str` returns `base64(payload).signature` where payload holds expiry and a random nonce.
- `verify_session(token: str) -> bool` validates signature and expiry.
- `new_csrf_token(session: str) -> str` deterministic HMAC of session + secret.
- `verify_csrf(session: str, token: str) -> bool`.

- [ ] **Step 1: Write failing tests**

Test correct/wrong password, valid/expired/tampered session, and CSRF accept/reject.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/test_auth.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement auth**

Use `hmac.new(secret.encode(), payload, hashlib.sha256)` and `base64.urlsafe_b64encode`. Reject malformed tokens. Never store the password in the cookie.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_auth.py -q`
Expected: PASS.

---

### Task 6: Provider, service, and job worker

**Files:**
- Create: `runpod_ops/provider.py`
- Create: `runpod_ops/service.py`
- Create: `runpod_ops/jobs.py`
- Test: `tests/test_service.py`, `tests/test_jobs.py`

**Interfaces:**
- `PodProvider` protocol: `get_pod(pod_id) -> dict`, `list_gpus(min_mem, min_vcpu, max_price) -> list[dict]`.
- `RunPodProvider` adapts engine `CloudManager` methods; returns plain dicts.
- `Service(config, state, provider)`:
  - `build_command(profile, action) -> list[str]`
  - `sync_pod_id(profile) -> str | None`
  - `queue_or_run(profile, action, actor)`.
- Commands invoke `.venv/bin/python -m runpod_ops.engine` in dev; `sys.executable` in production. Actions:
  - `start`: `<pod_id> start --json`
  - `stop`: `<pod_id> stop --json`
  - `terminate`: `<pod_id> remove --json`
  - `recreate`: `restart-or-recreate --pod-id <id> --pod-id-file <data>/pods/<name>.pod_id --fallback-template-id ... --json`
- `recreate` passes limits, preferred GPU, fallback name, volume/image when set, and notifier flags/values from config.
- `JobRunner(state, service, notifier=None)` starts one daemon worker thread, `submit(profile, action, actor) -> job_id`, and rejects a second active job for the same pod. It runs commands with `subprocess.Popen`, appends stdout/stderr lines to state, masks secrets in stored commands, and calls `sync_pod_id` after success.
- First line of every stored command must be redacted: tokens, passwords, API keys replaced with `***`.

- [ ] **Step 1: Write failing tests**

Test command construction for each action with and without pod ID/volume, pod-ID sync, redaction, successful job execution via a fake command, failure exit code, and duplicate active job rejection.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/test_service.py tests/test_jobs.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement provider/service/jobs**

Keep subprocess execution injectable (`command_runner` or `popen_factory`) for tests. Use one lock per pod plus an active-job set. Append each line as it arrives. On completion, update job and pod status. Never log environment values.

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_service.py tests/test_jobs.py -q`
Expected: PASS.

---

### Task 7: Optional SMTP notifier

**Files:**
- Create: `runpod_ops/notifiers.py`
- Test: `tests/test_notifiers.py`

**Interfaces:**
- `EmailNotifier(smtp: SMTPConfig)` with `send_job_result(pod, action, status, job_id) -> bool`.
- Uses `smtplib.SMTP`/`SMTP_SSL`, `starttls` when configured, timeouts, and `EmailMessage`.
- Disabled or incomplete config returns `False` without raising.
- `send` is injectable for tests.

- [ ] **Step 1: Write failing tests** for disabled config, missing recipient, and message construction/send call.
- [ ] **Step 2: Run tests and confirm failure** with `.venv/bin/pytest tests/test_notifiers.py -q`.
- [ ] **Step 3: Implement notifier** with no secrets in logs/messages.
- [ ] **Step 4: Re-run tests** and confirm PASS.

---

### Task 8: CLI

**Files:**
- Create: `runpod_ops/cli.py`
- Test: `tests/test_cli.py`
- Modify: `pyproject.toml` console script if not already present.

**Interfaces:**
- `main(argv: list[str] | None = None) -> int`.
- Subcommands: `serve`, `pods`, `status`, `start`, `stop`, `recreate`, `terminate`, `gpus`, `jobs`.
- `--config PATH` global; `serve` honors `--host/--port`.
- Mutations run synchronously via the service/runner and return 0/1. `status` and `pods` use state plus provider when possible.
- CLI output is plain text; errors go to stderr.

- [ ] **Step 1: Write failing tests** for argument parsing, unknown pod, start command invocation, and `serve` delegation via monkeypatch.
- [ ] **Step 2: Run tests and confirm failure** with `.venv/bin/pytest tests/test_cli.py -q`.
- [ ] **Step 3: Implement CLI** with `argparse`; reuse `load_config`, `StateStore`, `Service`, `JobRunner`.
- [ ] **Step 4: Re-run tests** and confirm PASS.

---

### Task 9: Web application and UI

**Files:**
- Create: `runpod_ops/web/__init__.py`
- Create: `runpod_ops/web/app.py`
- Create: `runpod_ops/web/routes.py`
- Create: `runpod_ops/web/templates/base.html`
- Create: `runpod_ops/web/templates/login.html`
- Create: `runpod_ops/web/templates/dashboard.html`
- Create: `runpod_ops/web/templates/pod.html`
- Create: `runpod_ops/web/templates/job.html`
- Create: `runpod_ops/web/templates/_log.html`
- Create: `runpod_ops/web/static/app.css`
- Test: `tests/test_web.py`

**Interfaces:**
- `create_app(config: AppConfig, state: StateStore | None = None, runner: JobRunner | None = None, provider: PodProvider | None = None, auth: AuthManager | None = None) -> FastAPI`.
- Routes as listed in the spec: login/logout, dashboard, pod detail, mutations, job detail/log, health, JSON pod APIs.
- Session cookie is `HttpOnly`, `SameSite=Lax`; `Secure` when `RUNPOD_OPS_SECURE_COOKIES=true`.
- Login failure returns 401 with the form; mutation without CSRF returns 403; unauthenticated HTML routes redirect to `/login`; JSON routes return 401.
- Action buttons submit `pod`, `action`, and `csrf_token` form fields.
- Job log fragment polls every 2 seconds until job is finished.

- [ ] **Step 1: Write failing tests** with FastAPI `TestClient`: health unauthenticated, dashboard redirect, login success/failure, CSRF enforcement, mutation queues a job, duplicate action returns 409, pod detail renders name, log fragment returns lines.
- [ ] **Step 2: Run tests and confirm failure** with `.venv/bin/pytest tests/test_web.py -q`.
- [ ] **Step 3: Implement app factory, routes, templates, and CSS**.

Use server-rendered HTML and minimal inline JS/HTMX-free polling where possible. Do not introduce a JS build step. Keep templates small and readable.

- [ ] **Step 4: Re-run tests** and confirm PASS.

---

### Task 10: Packaging, Docker, compose, docs, CI, and legacy isolation

**Files:**
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `config.example.yaml`
- Create: `.github/workflows/ci.yml` (or `.gitlab-ci.yml` matching hosting; default GitHub)
- Create: `tests/test_packaging.py` (static checks)
- Modify: `README.md` (product quickstart and config reference)
- Create: `SECURITY.md`, `CONTRIBUTING.md`, `LICENSE` (MIT unless operator prefers otherwise)
- Move: v1-excluded scripts (`start.sh`, `stop.sh`, `info.sh`, `update-webservice.sh`, `test_alerts.sh`, `llm_lifecycle.py`, `dry_run_restart.py`, `config.sh.tpl`) into `legacy/` with a README; do not delete history.
- Modify: `.gitignore` for `.env`, `config.yaml`, `data/`.

**Interfaces:**
- Docker image runs as non-root, installs the package, installs `runpodctl`, exposes 8080, healthcheck hits `/api/health`, and starts `runpod-ops serve`.
- Compose mounts `./data:/data`, reads `.env`, restarts unless stopped.
- CI runs `.venv`-equivalent install plus `pytest` and a lint/compile check.

- [ ] **Step 1: Write packaging tests** asserting required files exist, Dockerfile has healthcheck/non-root, compose mounts data, examples contain no real secrets, and `.gitignore` covers `.env`/`config.yaml`/`data/`.
- [ ] **Step 2: Run tests and confirm failure** with `.venv/bin/pytest tests/test_packaging.py -q`.
- [ ] **Step 3: Implement packaging and docs**; move legacy files.
- [ ] **Step 4: Run full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Docker smoke test (if daemon available)**

Run: `docker build -t runpod-ops:dev .`
Expected: successful build.
Run: `docker run --rm -e ADMIN_PASSWORD=test -e SECRET_KEY=test -p 18080:8080 runpod-ops:dev` and curl `/api/health`.
Expected: `{"status":"ok"}`.

---

### Task 11: Final verification

- [ ] **Step 1: Run full tests** with `.venv/bin/pytest -q`; record output.
- [ ] **Step 2: Run compile check** with `.venv/bin/python -m compileall -q runpod_ops`.
- [ ] **Step 3: Grep for leaked secrets/vendor identifiers** in tracked files: `techfusion`, `drop.co`, hardcoded API keys, `Hetzner`, `danilo.santoro`.
- [ ] **Step 4: Verify git status** contains only intended changes; do not commit unless asked.
- [ ] **Step 5: Report** files created, tests run, Docker result, and any deferred follow-ups.
