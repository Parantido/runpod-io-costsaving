import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.config import AppConfig, PodProfile
from runpod_ops.jobs import JobConflict
from runpod_ops.scheduler import (
    ScheduleError,
    ScheduleExecutor,
    Scheduler,
    next_run,
    validate_cron,
)
from runpod_ops.state import StateStore


class FakeService:
    @staticmethod
    def redact_command(argv):
        return " ".join(argv)


class FakeRunner:
    def __init__(self, status="succeeded", conflict=False, order=None):
        self.service = FakeService()
        self.status = status
        self.conflict = conflict
        self.order = order if order is not None else []
        self.submitted = []

    def submit(self, profile, action, actor):
        if self.conflict:
            raise JobConflict("busy")
        self.order.append("submit")
        self.submitted.append((profile.name, action, actor))
        return "job-1"

    def wait_for(self, job_id, timeout=None):
        self.order.append("wait")
        return self.status


def _setup(tmp_path, **schedule_kwargs):
    profile = PodProfile(name="riva", template_id="tpl-1")
    config = AppConfig(data_dir=tmp_path, pods=(profile,), secret_key="s")
    state = StateStore(tmp_path / "state.db")
    state.init_schema()
    runner = schedule_kwargs.pop("runner", FakeRunner())
    executor = ScheduleExecutor(state, config, runner, timeout=1)
    defaults = dict(
        name="riva-up",
        pod="riva",
        action="recreate",
        cron="15 12 * * 1-6",
        trigger="app",
        enabled=True,
    )
    defaults.update(schedule_kwargs)
    schedule_id = state.create_schedule(**defaults)
    return state, executor, runner, state.get_schedule(schedule_id)


def test_validate_cron_rejects_bad_expression():
    with pytest.raises(ScheduleError):
        validate_cron("15 12 * *")
    with pytest.raises(ScheduleError):
        validate_cron("not a cron")
    validate_cron("15 12 * * 1-6")


def test_next_run_uses_standard_cron_days():
    monday = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    assert next_run("15 12 * * 1-6", base=monday).startswith("2026-11-16T12:15")
    friday = datetime(2026, 11, 20, 12, 16, tzinfo=timezone.utc)
    assert next_run("15 12 * * 1-6", base=friday).startswith("2026-11-21T12:15")


def test_scheduler_skips_disabled_and_external(tmp_path):
    state, executor, runner, schedule = _setup(tmp_path, enabled=False)
    scheduler = Scheduler(state, executor, now_provider=lambda: datetime(2026, 11, 16, 12, 15, tzinfo=timezone.utc))
    scheduler.tick()
    scheduler.wait_idle()
    assert runner.submitted == []

    state.update_schedule(schedule["id"], enabled=True, trigger="external")
    scheduler.tick()
    scheduler.wait_idle()
    assert runner.submitted == []


def test_scheduler_runs_app_schedule_once_per_minute(tmp_path):
    state, executor, runner, schedule = _setup(tmp_path, cron="* * * * *")
    scheduler = Scheduler(
        state, executor, now_provider=lambda: datetime.now(timezone.utc)
    )
    scheduler.tick()
    assert scheduler.wait_idle()
    assert runner.submitted == [("riva", "recreate", "scheduler:riva-up")]

    scheduler.tick()
    assert scheduler.wait_idle()
    assert len(runner.submitted) == 1


def test_scheduler_does_not_run_on_sunday(tmp_path):
    state, executor, runner, _ = _setup(tmp_path)
    sunday = datetime(2026, 11, 15, 12, 15, tzinfo=timezone.utc)
    scheduler = Scheduler(state, executor, now_provider=lambda: sunday)
    scheduler.tick()
    assert scheduler.wait_idle()
    assert runner.submitted == []


def test_executor_runs_hooks_in_order_and_records(tmp_path):
    order = []

    def command_runner(argv, on_line):
        order.append(argv[0])
        on_line("hook output")
        return 0

    runner = FakeRunner(order=order)
    state, executor, runner, schedule = _setup(
        tmp_path,
        runner=runner,
        pre_hooks=[["pre.sh"]],
        post_hooks=[["post.sh"]],
    )
    executor.command_runner = command_runner
    result = executor.run(schedule)
    assert result["status"] == "succeeded"
    assert order == ["pre.sh", "submit", "wait", "post.sh"]
    saved = state.get_schedule(schedule["id"])
    assert saved["last_status"] == "succeeded"
    assert saved["last_job_id"] == "job-1"
    assert len(state.list_jobs(pod="riva")) == 2


def test_executor_aborts_on_pre_hook_failure(tmp_path):
    order = []
    state, executor, runner, schedule = _setup(
        tmp_path, runner=FakeRunner(order=order), pre_hooks=[["pre.sh"]]
    )
    executor.command_runner = lambda argv, on_line: 1
    result = executor.run(schedule)
    assert result["status"] == "hook_failed"
    assert "submit" not in order
    assert state.get_schedule(schedule["id"])["last_status"] == "hook_failed"


def test_executor_marks_post_hook_failure(tmp_path):
    state, executor, runner, schedule = _setup(
        tmp_path, post_hooks=[["post.sh"]]
    )
    executor.command_runner = lambda argv, on_line: 2
    result = executor.run(schedule)
    assert result["status"] == "hook_failed"


def test_executor_marks_conflict_as_skipped(tmp_path):
    state, executor, runner, schedule = _setup(
        tmp_path, runner=FakeRunner(conflict=True)
    )
    result = executor.run(schedule)
    assert result["status"] == "skipped"
    assert state.get_schedule(schedule["id"])["last_status"] == "skipped"


def test_executor_marks_unknown_pod_invalid(tmp_path):
    state, executor, runner, schedule = _setup(tmp_path, pod="missing")
    result = executor.run(schedule)
    assert result["status"] == "invalid"
