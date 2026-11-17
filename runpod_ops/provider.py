"""Cloud provider interface and the RunPod implementation."""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PodProvider(Protocol):
    def get_pod(self, pod_id: str) -> dict[str, Any]: ...

    def list_gpus(
        self, min_mem: int, min_vcpu: int, max_price: float
    ) -> list[dict[str, Any]]: ...


class RunPodProvider:
    def __init__(self, api_key_file: str = ""):
        if api_key_file and not os.environ.get("RUNPOD_API_KEY"):
            os.environ["RUNPOD_API_KEY_FILE"] = api_key_file

    def _cloud(self):
        from runpod_ops.engine import CloudManager

        return CloudManager()

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        info = self._cloud().get_pod_info_graphql(pod_id)
        if isinstance(info, dict):
            return info
        return asdict(info)

    def list_gpus(
        self, min_mem: int, min_vcpu: int, max_price: float
    ) -> list[dict[str, Any]]:
        gpus = self._cloud().get_all_available_gpus()
        eligible = [
            asdict(gpu)
            for gpu in gpus
            if gpu.matches_requirements(min_mem, min_vcpu, max_price)
        ]
        return sorted(eligible, key=lambda gpu: gpu.get("ondemand_price") or float("inf"))
