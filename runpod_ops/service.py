"""Adapts pod profiles to engine commands and synchronizes pod state."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from runpod_ops.config import AppConfig, PodProfile
from runpod_ops.state import StateStore


class ServiceError(RuntimeError):
    pass


_SECRET_FLAGS = {
    "--telegram-token",
    "--password",
    "--token",
    "--secret",
    "--api-key",
}


class Service:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        python_executable: str | None = None,
        engine_module: str = "runpod_ops.engine",
    ):
        self.config = config
        self.state = state
        self.python = python_executable or sys.executable
        self.engine_module = engine_module

    def pod_id_file(self, profile: PodProfile) -> Path:
        return Path(self.config.data_dir) / "pods" / f"{profile.name}.pod_id"

    def stored_pod_id(self, profile: PodProfile) -> str | None:
        path = self.pod_id_file(profile)
        try:
            if path.is_file():
                value = path.read_text().strip()
                if value:
                    return value
        except OSError:
            pass
        row = self.state.get_pod(profile.name)
        return row.get("pod_id") if row else None

    def sync_pod_id(self, profile: PodProfile) -> str | None:
        path = self.pod_id_file(profile)
        try:
            pod_id = path.read_text().strip() if path.is_file() else ""
        except OSError:
            pod_id = ""
        if not pod_id:
            return self.stored_pod_id(profile)
        existing = self.state.get_pod(profile.name)
        status = existing["status"] if existing and existing.get("status") else "UNKNOWN"
        self.state.upsert_pod(profile.name, pod_id, status)
        return pod_id

    @staticmethod
    def status_after(action: str) -> str:
        return {
            "start": "RUNNING",
            "recreate": "RUNNING",
            "stop": "STOPPED",
            "terminate": "TERMINATED",
        }.get(action, "UNKNOWN")

    def build_command(self, profile: PodProfile, action: str) -> list[str]:
        if action not in {"start", "stop", "terminate", "recreate"}:
            raise ServiceError(f"Unsupported action: {action}")

        pod_id = self.stored_pod_id(profile)

        if action in {"start", "stop", "terminate"}:
            if not pod_id:
                raise ServiceError(f"Pod {profile.name!r} has no pod ID; run recreate first")
            engine_action = "remove" if action == "terminate" else action
            return [
                self.python,
                "-m",
                self.engine_module,
                engine_action,
                pod_id,
                "--json",
            ]

        command = [
            self.python,
            "-m",
            self.engine_module,
            "restart-or-recreate",
        ]
        if pod_id:
            command.append(pod_id)
        command.extend(
            [
                "--pod-id-file",
                str(self.pod_id_file(profile)),
                "--force-recreate",
                "--fallback-template-id",
                profile.template_id,
                "--fallback-name",
                profile.fallback_name or profile.name,
                "--fallback-min-mem",
                str(profile.min_gpu_mem_gb),
                "--fallback-min-vcpu",
                str(profile.min_vcpu),
                "--max-price",
                str(profile.max_price),
                "--search-timeout",
                str(profile.search_timeout),
                "--poll-interval",
                str(profile.poll_interval),
            ]
        )
        if profile.network_volume_id:
            command.extend(["--fallback-network-volume-id", profile.network_volume_id])
        if profile.image_name:
            command.extend(["--fallback-image-name", profile.image_name])
        if profile.preferred_gpu:
            command.extend(["--fallback-gpu", profile.preferred_gpu])

        notifications = self.config.notifications
        if (
            notifications.telegram.enabled
            and notifications.telegram.bot_token
            and notifications.telegram.chat_id
        ):
            command.extend(
                [
                    "--telegram-token",
                    notifications.telegram.bot_token,
                    "--telegram-chat-id",
                    notifications.telegram.chat_id,
                ]
            )
        else:
            command.append("--no-telegram")
        if notifications.loki.enabled and notifications.loki.url:
            command.extend(["--loki-url", notifications.loki.url])
        else:
            command.append("--no-loki")

        command.append("--json")
        return command

    def redact_command(self, command: list[str]) -> str:
        redacted: list[str] = []
        hide_next = False
        for part in command:
            if hide_next:
                redacted.append("***")
                hide_next = False
                continue
            redacted.append(part)
            if part in _SECRET_FLAGS:
                hide_next = True
        return " ".join(redacted)

    def record_status(self, profile: PodProfile, action: str) -> None:
        row = self.state.get_pod(profile.name)
        pod_id = self.sync_pod_id(profile)
        if row or pod_id:
            self.state.upsert_pod(
                profile.name, pod_id, self.status_after(action)
            )
