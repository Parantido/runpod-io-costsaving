# Contributing

## Development Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Rules

- Keep the engine's failover behavior unchanged unless the change is covered
  by tests.
- Add a failing test before changing behavior.
- Never commit secrets, tenant identifiers, generated `data/`, or `.env`.
- Keep the web UI dependency-free: server-rendered HTML plus small vanilla
  JavaScript; no build step.
- Update `config.example.yaml` and `README.md` when configuration changes.

## Pull Requests

- Include the commands run and their results.
- Keep changes scoped; separate refactors from behavior changes.
- Confirm `docker build -t runpod-ops:dev .` when packaging changes.
