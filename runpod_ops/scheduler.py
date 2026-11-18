"""Cron schedules, hook execution, and the in-process scheduler thread."""

from __future__ import annotations

import logging
import os
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from croniter import croniter

from runpod_ops.config import AppConfig
from runpod_ops.jobs import CommandRunner, JobConflict, JobRunner, _subprocess_runner
from runpod_ops.state import StateStore

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"start", "stop", "recreate", "terminate"}
VALID_TRIGGERS = {"external", "app"}


class ScheduleError(ValueError):
    pass


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:
        raise ScheduleError(f"Unknown timezone: {timezone_name}") from exc


def validate_cron(expr: str, timezone_name: str = "Etc/UTC") -> None:
    if not expr or len(expr.split()) != 5:
        raise ScheduleError("Cron expression must have five fields")
    zone = _zone(timezone_name)
    base = datetime.now(zone).replace(tzinfo=None)
    try:
        croniter(expr, base).get_next(datetime)
    except Exception as exc:
        raise ScheduleError(f"Invalid cron expression: {expr}") from exc


def next_run(
    expr: str, timezone_name: str = "Etc/UTC", base: datetime | None = None
) -> str:
    zone = _zone(timezone_name)
    base_utc = base or datetime.now(timezone.utc)
    if base_utc.tzinfo is None:
        base_utc = base_utc.replace(tzinfo=timezone.utc)
    local = base_utc.astimezone(zone).replace(tzinfo=None)
    try:
        upcoming = croniter(expr, local).get_next(datetime)
    except Exception as exc:
        raise ScheduleError(f"Invalid cron expression: {expr}") from exc
    return upcoming.replace(tzinfo=zone).astimezone(timezone.utc).isoformat()


def _hook_argv(hook: Any) -> list[str]:
    if isinstance(hook, str):
        argv = shlex.split(hook)
    elif isinstance(hook, (list, tuple)):
        argv = [str(part) for part in hook]
    else:
        raise ScheduleError(f"Invalid hook definition: {hook!r}")
    if not argv:
        raise ScheduleError("Hook command is empty")
    root = os.environ.get("RUNPOD_OPS_ROOT") or str(
        Path(__file__).resolve().parent.parent
    )
    return [part.replace("{root}", root) for part in argv]


class ScheduleExecutor:
    def __init__(
        self,
        state: StateStore,
        config: AppConfig,
        runner: JobRunner,
        command_runner: CommandRunner | None = None,
        timeout: float = 3600.0,
    ):
        self.state = state
        self.config = config
        self.runner = runner
        self.command_runner = command_runner or _subprocess_runner
        self.timeout = timeout

    def _run_hook(self, schedule: dict[str, Any], hook: Any, phase: str) -> tuple[str, str]:
        argv = _hook_argv(hook)
        pod = schedule["pod"]
        job_id = self.state.create_job(
            pod,
            f"{phase}_hook",
            self.runner.service.redact_command(argv),
        )
        self.state.start_job(job_id)
        try:
            exit_code = self.command_runner(
                argv, lambda line: self.state.add_log(job_id, line)
            )
        except Exception as exc:
            self.state.add_log(job_id, str(exc))
            exit_code = 1
        status = "succeeded" if exit_code == 0 else "failed"
        self.state.finish_job(job_id, status, exit_code)
        self.state.audit(
            f"schedule:{schedule['name']}",
            f"{phase}_hook",
            pod,
            f"job {job_id} {status}",
        )
        return job_id, status

    def run(self, schedule: dict[str, Any], actor: str = "schedule") -> dict[str, Any]:
        schedule_id = int(schedule["id"])
        pod = schedule["pod"]
        next_at = None
        try:
            next_at = next_run(schedule["cron"], schedule["timezone"])
        except ScheduleError:
            pass

        try:
            profile = self.config.get_pod(pod)
        except KeyError:
            self.state.record_schedule_run(
                schedule_id, None, "invalid", next_at
            )
            self.state.audit(actor, "schedule", pod, "unknown pod")
            return {"status": "invalid", "job_ids": []}

        job_ids: list[str] = []
        for hook in schedule.get("pre_hooks") or []:
            hook_job, status = self._run_hook(schedule, hook, "pre")
            job_ids.append(hook_job)
            if status != "succeeded":
                self.state.record_schedule_run(
                    schedule_id, hook_job, "hook_failed", next_at
                )
                return {
                    "status": "hook_failed",
                    "job_id": hook_job,
                    "job_ids": job_ids,
                }

        try:
            job_id = self.runner.submit(
                profile, schedule["action"], actor
            )
        except JobConflict:
            self.state.record_schedule_run(schedule_id, None, "skipped", next_at)
            self.state.audit(actor, "schedule", pod, "job conflict")
            return {"status": "skipped", "job_ids": job_ids}

        job_ids.append(job_id)
        status = self.runner.wait_for(job_id, timeout=self.timeout)

        if status == "succeeded":
            for hook in schedule.get("post_hooks") or []:
                hook_job, hook_status = self._run_hook(schedule, hook, "post")
                job_ids.append(hook_job)
                if hook_status != "succeeded":
                    status = "hook_failed"
                    break

        self.state.record_schedule_run(schedule_id, job_id, status, next_at)
        return {"status": status, "job_id": job_id, "job_ids": job_ids}


class Scheduler:
    def __init__(
        self,
        state: StateStore,
        executor: ScheduleExecutor,
        interval: float = 20.0,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.state = state
        self.executor = executor
        self.interval = interval
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler tick failed")

    def tick(self, now: datetime | None = None) -> None:
        now_utc = now or self.now_provider()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        for schedule in self.state.list_schedules():
            if schedule["trigger"] != "app" or not schedule["enabled"]:
                continue
            try:
                zone = _zone(schedule["timezone"])
                local = now_utc.astimezone(zone)
                due = croniter.match(
                    schedule["cron"], local.replace(tzinfo=None)
                )
                upcoming = next_run(schedule["cron"], schedule["timezone"], now_utc)
                self.state.set_schedule_next_run(int(schedule["id"]), upcoming)
            except Exception:
                logger.exception("invalid schedule %s", schedule.get("name"))
                continue
            if not due:
                continue
            last = schedule.get("last_run_at")
            if last:
                try:
                    last_local = datetime.fromisoformat(last).astimezone(zone)
                    if last_local.strftime("%Y%m%d%H%M") == local.strftime("%Y%m%d%H%M"):
                        continue
                except ValueError:
                    pass
            with self._lock:
                if schedule["name"] in self._running:
                    continue
                self._running.add(schedule["name"])
            threading.Thread(
                target=self._dispatch,
                args=(schedule,),
                daemon=True,
            ).start()

    def _dispatch(self, schedule: dict[str, Any]) -> None:
        try:
            self.executor.run(schedule, actor=f"scheduler:{schedule['name']}")
        except Exception:
            logger.exception("scheduled run failed: %s", schedule.get("name"))
        finally:
            with self._lock:
                self._running.discard(schedule["name"])

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._running:
                    return True
            time.sleep(0.02)
        return False
