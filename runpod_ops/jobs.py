"""In-process job queue and engine command runner."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from typing import Callable, Iterable

from runpod_ops.config import PodProfile
from runpod_ops.locks import PodLockManager
from runpod_ops.service import Service
from runpod_ops.state import StateStore

logger = logging.getLogger(__name__)

CommandRunner = Callable[[list[str], Callable[[str], None]], int]


class JobConflict(RuntimeError):
    pass


def _subprocess_runner(argv: list[str], on_line: Callable[[str], None]) -> int:
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        on_line(str(exc))
        return 127
    assert process.stdout is not None
    for line in process.stdout:
        on_line(line.rstrip())
    return process.wait()


class JobRunner:
    def __init__(
        self,
        state: StateStore,
        service: Service,
        notifier=None,
        command_runner: CommandRunner | None = None,
        poll_interval: float = 0.2,
        lock_dir=None,
    ):
        self.state = state
        self.service = service
        self.notifier = notifier
        self.command_runner = command_runner or _subprocess_runner
        self.poll_interval = poll_interval
        self.pod_locks = PodLockManager(
            lock_dir or service.config.data_dir / "locks"
        )
        self._queue: queue.Queue = queue.Queue()
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def submit(self, profile: PodProfile, action: str, actor: str) -> str:
        command = self.service.build_command(profile, action)
        with self._lock:
            if profile.name in self._active:
                raise JobConflict(f"Pod {profile.name!r} already has an active job")
        if not self.pod_locks.acquire(profile.name):
            raise JobConflict(
                f"Pod {profile.name!r} is locked by another process"
            )
        with self._lock:
            self._active.add(profile.name)
        try:
            job_id = self.state.create_job(
                profile.name, action, self.service.redact_command(command)
            )
            self.state.audit(actor, action, profile.name, f"queued job {job_id}")
        except Exception:
            self._release(profile.name)
            raise
        self._queue.put((job_id, profile, action, command))
        return job_id

    def wait_for(self, job_id: str, timeout: float = 60.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.state.get_job(job_id)
            if job and job["status"] in {"succeeded", "failed"}:
                return job["status"]
            time.sleep(self.poll_interval)
        return "timeout"

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, profile, action, command = item
            try:
                self._run(job_id, profile, action, command)
            except Exception:
                logger.exception("job %s crashed", job_id)
                self.state.add_log(job_id, "internal error while running job")
                self.state.finish_job(job_id, "failed", 1)
            finally:
                self._release(profile.name)

    def _release(self, pod_name: str) -> None:
        with self._lock:
            self._active.discard(pod_name)
        self.pod_locks.release(pod_name)

    def _run(
        self, job_id: str, profile: PodProfile, action: str, command: list[str]
    ) -> None:
        self.state.start_job(job_id)
        exit_code = self.command_runner(
            command, lambda line: self.state.add_log(job_id, line)
        )
        self._release(profile.name)
        status = "succeeded" if exit_code == 0 else "failed"
        if status == "succeeded":
            self.service.record_status(profile, action)
            self.state.audit("system", action, profile.name, f"job {job_id} succeeded")
        else:
            self.state.audit(
                "system", action, profile.name, f"job {job_id} failed ({exit_code})"
            )
        if self.notifier is not None:
            try:
                self.notifier.send_job_result(
                    profile.name, action, status, job_id
                )
            except Exception:
                logger.exception("notifier failed for job %s", job_id)
        self.state.finish_job(job_id, status, exit_code)
