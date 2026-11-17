"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from runpod_ops.auth import AuthManager
from runpod_ops.config import AppConfig
from runpod_ops.jobs import JobRunner
from runpod_ops.notifiers import EmailNotifier
from runpod_ops.provider import PodProvider, RunPodProvider
from runpod_ops.service import Service
from runpod_ops.state import StateStore
from runpod_ops.web.routes import register_routes

_TEMPLATES = Path(__file__).parent / "templates"
_STATIC = Path(__file__).parent / "static"


def create_app(
    config: AppConfig,
    state: StateStore | None = None,
    runner: JobRunner | None = None,
    provider: PodProvider | None = None,
    auth: AuthManager | None = None,
) -> FastAPI:
    state = state or StateStore(config.data_dir / "runpod_ops.db")
    state.init_schema()

    if runner is None:
        service = Service(config, state)
        notifier = EmailNotifier(config.notifications.smtp)
        if not config.notifications.smtp.enabled:
            notifier = None
        runner = JobRunner(state, service, notifier=notifier)

    provider = provider or RunPodProvider(config.runpod.api_key_file)
    auth = auth or AuthManager(config.admin_password, config.secret_key)

    app = FastAPI(title="runpod-ops", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.store = state
    app.state.runner = runner
    app.state.provider = provider
    app.state.auth = auth
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    templates = Jinja2Templates(directory=str(_TEMPLATES))
    register_routes(app, config, state, runner, provider, auth, templates)
    return app
