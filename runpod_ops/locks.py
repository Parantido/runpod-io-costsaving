"""Cross-process per-pod locks shared by host CLI, scheduler, and container."""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path
from typing import IO


class PodLockManager:
    def __init__(self, lock_dir: str | Path):
        self.lock_dir = Path(lock_dir)
        self._handles: dict[str, IO[str]] = {}
        self._lock = threading.Lock()

    def acquire(self, pod: str) -> bool:
        with self._lock:
            if pod in self._handles:
                return False
            self.lock_dir.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_dir / f"{pod}.lock", "a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return False
            handle.seek(0)
            handle.truncate()
            handle.write(str(threading.get_ident()))
            handle.flush()
            self._handles[pod] = handle
            return True

    def release(self, pod: str) -> None:
        with self._lock:
            handle = self._handles.pop(pod, None)
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
