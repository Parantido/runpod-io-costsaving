"""HTTP routes for the runpod-ops web UI."""

from __future__ import annotations

import threading

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from runpod_ops.auth import AuthManager
from runpod_ops.config import AppConfig
from runpod_ops.jobs import JobConflict, JobRunner
from runpod_ops.provider import PodProvider
from runpod_ops.scheduler import (
    VALID_TRIGGERS,
    ScheduleError,
    ScheduleExecutor,
    next_run,
    validate_cron,
)
from runpod_ops.state import StateStore

_SESSION_COOKIE = "session"
_ACTIONS = {"start", "stop", "recreate", "terminate"}


def register_routes(
    app: FastAPI,
    config: AppConfig,
    state: StateStore,
    runner: JobRunner,
    provider: PodProvider,
    auth: AuthManager,
    templates: Jinja2Templates,
    executor: ScheduleExecutor,
) -> None:
    def session(request: Request) -> str:
        return request.cookies.get(_SESSION_COOKIE, "")

    def authenticated(request: Request) -> bool:
        return auth.verify_session(session(request))

    def context(request: Request, **extra) -> dict:
        token = session(request)
        csrf = auth.new_csrf_token(token) if token else ""
        base = {"request": request, "config": config, "csrf_token": csrf}
        base.update(extra)
        return base

    def html(request: Request, name: str, status_code: int = 200, **extra):
        return templates.TemplateResponse(
            request, name, context(request, **extra), status_code=status_code
        )

    def require_html(request: Request):
        if not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return None

    def require_api(request: Request) -> None:
        if not authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")

    def require_csrf(request: Request, token: str) -> None:
        if not authenticated(request) or not auth.verify_csrf(session(request), token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")

    def pod_or_404(name: str):
        try:
            return config.get_pod(name)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown pod")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if authenticated(request):
            return RedirectResponse("/", status_code=303)
        return html(request, "login.html", csrf_token="")

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        if not auth.check_password(password):
            return html(
                request,
                "login.html",
                status_code=401,
                error="Invalid password",
                csrf_token="",
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            _SESSION_COOKIE,
            auth.create_session(),
            httponly=True,
            samesite="lax",
            secure=config.server.secure_cookies,
            max_age=auth.ttl_seconds,
        )
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        require_csrf(request, csrf_token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(_SESSION_COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        redirect = require_html(request)
        if redirect:
            return redirect
        stored = {row["name"]: row for row in state.list_pods()}
        pods = []
        for profile in config.pods:
            row = stored.get(profile.name, {})
            jobs = state.list_jobs(pod=profile.name, limit=1)
            pods.append(
                {
                    "profile": profile,
                    "pod_id": row.get("pod_id"),
                    "status": row.get("status") or "UNKNOWN",
                    "last_job": jobs[0] if jobs else None,
                }
            )
        return html(request, "dashboard.html", pods=pods)

    @app.get("/pods/{name}", response_class=HTMLResponse)
    def pod_detail(request: Request, name: str):
        redirect = require_html(request)
        if redirect:
            return redirect
        profile = pod_or_404(name)
        row = state.get_pod(name) or {}
        jobs = state.list_jobs(pod=name, limit=20)
        return html(
            request,
            "pod.html",
            profile=profile,
            pod_id=row.get("pod_id"),
            status=row.get("status") or "UNKNOWN",
            jobs=jobs,
        )

    @app.post("/pods/{name}/{action}")
    def pod_action(
        request: Request,
        name: str,
        action: str,
        csrf_token: str = Form(""),
    ):
        require_csrf(request, csrf_token)
        if action not in _ACTIONS:
            raise HTTPException(status_code=404, detail="unknown action")
        profile = pod_or_404(name)
        try:
            job_id = runner.submit(profile, action, "admin")
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str):
        redirect = require_html(request)
        if redirect:
            return redirect
        job = state.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        return html(request, "job.html", job=job, logs=state.get_logs(job_id))

    @app.get("/jobs/{job_id}/log", response_class=HTMLResponse)
    def job_log(request: Request, job_id: str):
        redirect = require_html(request)
        if redirect:
            return redirect
        if not state.get_job(job_id):
            raise HTTPException(status_code=404, detail="unknown job")
        return html(request, "_log.html", logs=state.get_logs(job_id))

    def schedule_rows() -> list[dict]:
        rows = state.list_schedules()
        for item in rows:
            try:
                item["next_calc"] = next_run(item["cron"], item["timezone"])
            except ScheduleError:
                item["next_calc"] = "invalid"
        return rows

    def validate_schedule_form(
        pod: str, action: str, cron: str, timezone_name: str, trigger: str
    ) -> None:
        try:
            config.get_pod(pod)
        except KeyError as exc:
            raise ScheduleError(f"Unknown pod: {pod}") from exc
        if action not in _ACTIONS:
            raise ScheduleError(f"Unsupported action: {action}")
        if trigger not in VALID_TRIGGERS:
            raise ScheduleError(f"Unsupported trigger: {trigger}")
        validate_cron(cron, timezone_name)

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request, error: str = ""):
        redirect = require_html(request)
        if redirect:
            return redirect
        return html(
            request,
            "schedules.html",
            schedules=schedule_rows(),
            pods=config.pods,
            actions=sorted(_ACTIONS),
            triggers=sorted(VALID_TRIGGERS),
            error=error,
        )

    @app.post("/schedules")
    def schedule_create(
        request: Request,
        name: str = Form(""),
        pod: str = Form(""),
        action: str = Form(""),
        cron: str = Form(""),
        timezone_name: str = Form("Etc/UTC", alias="timezone"),
        trigger: str = Form("external"),
        enabled: str = Form(""),
        csrf_token: str = Form(""),
    ):
        require_csrf(request, csrf_token)
        try:
            validate_schedule_form(pod, action, cron, timezone_name, trigger)
            state.create_schedule(
                name.strip(),
                pod,
                action,
                cron,
                timezone_name,
                trigger,
                enabled == "on",
            )
            state.audit("admin", "schedule_create", pod, name)
        except Exception as exc:
            return html(
                request,
                "schedules.html",
                status_code=400,
                schedules=schedule_rows(),
                pods=config.pods,
                actions=sorted(_ACTIONS),
                triggers=sorted(VALID_TRIGGERS),
                error=str(exc),
            )
        return RedirectResponse("/schedules", status_code=303)

    @app.post("/schedules/{schedule_id}/edit")
    def schedule_edit(
        request: Request,
        schedule_id: int,
        name: str = Form(""),
        pod: str = Form(""),
        action: str = Form(""),
        cron: str = Form(""),
        timezone_name: str = Form("Etc/UTC", alias="timezone"),
        trigger: str = Form("external"),
        enabled: str = Form(""),
        csrf_token: str = Form(""),
    ):
        require_csrf(request, csrf_token)
        schedule = state.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail="unknown schedule")
        try:
            validate_schedule_form(pod, action, cron, timezone_name, trigger)
            state.update_schedule(
                schedule_id,
                name=name.strip(),
                pod=pod,
                action=action,
                cron=cron,
                timezone=timezone_name,
                trigger=trigger,
                enabled=enabled == "on",
            )
            state.audit("admin", "schedule_edit", pod, name)
        except Exception as exc:
            return html(
                request,
                "schedules.html",
                status_code=400,
                schedules=schedule_rows(),
                pods=config.pods,
                actions=sorted(_ACTIONS),
                triggers=sorted(VALID_TRIGGERS),
                error=str(exc),
            )
        return RedirectResponse("/schedules", status_code=303)

    @app.post("/schedules/{schedule_id}/toggle")
    def schedule_toggle(
        request: Request,
        schedule_id: int,
        csrf_token: str = Form(""),
    ):
        require_csrf(request, csrf_token)
        schedule = state.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail="unknown schedule")
        state.update_schedule(schedule_id, enabled=not schedule["enabled"])
        return RedirectResponse("/schedules", status_code=303)

    @app.post("/schedules/{schedule_id}/delete")
    def schedule_delete(
        request: Request,
        schedule_id: int,
        csrf_token: str = Form(""),
    ):
        require_csrf(request, csrf_token)
        if not state.get_schedule(schedule_id):
            raise HTTPException(status_code=404, detail="unknown schedule")
        state.delete_schedule(schedule_id)
        return RedirectResponse("/schedules", status_code=303)

    @app.post("/schedules/{schedule_id}/run")
    def schedule_run(
        request: Request,
        schedule_id: int,
        csrf_token: str = Form(""),
    ):
        require_csrf(request, csrf_token)
        schedule = state.get_schedule(schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail="unknown schedule")
        threading.Thread(
            target=executor.run,
            args=(schedule,),
            kwargs={"actor": "admin"},
            daemon=True,
        ).start()
        return RedirectResponse("/schedules", status_code=303)

    @app.get("/api/pods")
    def api_pods(request: Request):
        require_api(request)
        stored = {row["name"]: row for row in state.list_pods()}
        return [
            {
                "name": profile.name,
                "pod_id": stored.get(profile.name, {}).get("pod_id"),
                "status": stored.get(profile.name, {}).get("status") or "UNKNOWN",
                "template_id": profile.template_id,
            }
            for profile in config.pods
        ]

    @app.get("/api/pods/{name}")
    def api_pod(request: Request, name: str):
        require_api(request)
        profile = pod_or_404(name)
        row = state.get_pod(name) or {}
        result = {
            "name": name,
            "pod_id": row.get("pod_id"),
            "status": row.get("status") or "UNKNOWN",
            "template_id": profile.template_id,
        }
        if row.get("pod_id"):
            try:
                result["pod"] = provider.get_pod(row["pod_id"])
            except Exception as exc:  # noqa: BLE001 - surfaced to operator
                result["error"] = str(exc)
        return JSONResponse(result)
