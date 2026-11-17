import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.config import (
    AppConfig,
    NotificationConfig,
    PodProfile,
    TelegramConfig,
)
from runpod_ops.jobs import JobConflict, JobRunner
from runpod_ops.service import Service
from runpod_ops.state import StateStore


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def send_job_result(self, pod, action, status, job_id):
        self.calls.append((pod, action, status, job_id))
        return True


def _setup(tmp_path, runner_func, notifier=None):
    pod = PodProfile(name="riva", template_id="tpl-1")
    config = AppConfig(
        data_dir=tmp_path,
        pods=(pod,),
        secret_key="s",
        notifications=NotificationConfig(
            telegram=TelegramConfig(enabled=True, bot_token="secret-token", chat_id="1")
        ),
    )
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    service = Service(config, store)
    runner = JobRunner(
        store,
        service,
        notifier=notifier,
        command_runner=runner_func,
        poll_interval=0.01,
    )
    return runner, store, service, pod


def test_successful_job_streams_logs_and_updates_state(tmp_path):
    def run(argv, on_line):
        on_line("line one")
        on_line("line two")
        pod_file = tmp_path / "pods" / "riva.pod_id"
        pod_file.parent.mkdir(parents=True, exist_ok=True)
        pod_file.write_text("pod-new")
        return 0

    runner, store, service, pod = _setup(tmp_path, run)
    job_id = runner.submit(pod, "recreate", "tester")
    assert runner.wait_for(job_id, timeout=5) == "succeeded"

    job = store.get_job(job_id)
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert "secret-token" not in job["command"]
    assert "***" in job["command"]
    assert [entry["line"] for entry in store.get_logs(job_id)] == [
        "line one",
        "line two",
    ]
    assert store.get_pod("riva")["pod_id"] == "pod-new"
    assert store.list_audit()[0]["action"] == "recreate"


def test_failed_job_records_exit_code(tmp_path):
    def run(argv, on_line):
        on_line("boom")
        return 2

    runner, store, _, pod = _setup(tmp_path, run)
    job_id = runner.submit(pod, "recreate", "tester")
    assert runner.wait_for(job_id, timeout=5) == "failed"
    assert store.get_job(job_id)["exit_code"] == 2


def test_duplicate_active_job_rejected(tmp_path):
    gate = threading.Event()

    def run(argv, on_line):
        gate.wait(timeout=5)
        return 0

    runner, store, _, pod = _setup(tmp_path, run)
    first = runner.submit(pod, "recreate", "tester")
    with pytest.raises(JobConflict):
        runner.submit(pod, "recreate", "tester")
    gate.set()
    assert runner.wait_for(first, timeout=5) == "succeeded"
    second = runner.submit(pod, "recreate", "tester")
    assert runner.wait_for(second, timeout=5) == "succeeded"


def test_notifier_invoked_on_completion(tmp_path):
    notifier = FakeNotifier()

    def run(argv, on_line):
        return 0

    runner, _, _, pod = _setup(tmp_path, run, notifier=notifier)
    job_id = runner.submit(pod, "recreate", "tester")
    runner.wait_for(job_id, timeout=5)
    assert notifier.calls == [("riva", "recreate", "succeeded", job_id)]
