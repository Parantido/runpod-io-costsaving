"""Command line interface for runpod-ops."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Callable

from runpod_ops.config import AppConfig, load_config
from runpod_ops.jobs import JobRunner
from runpod_ops.notifiers import EmailNotifier
from runpod_ops.provider import RunPodProvider
from runpod_ops.service import Service
from runpod_ops.state import StateStore

_MUTATIONS = ("start", "stop", "recreate", "terminate")


@dataclass
class Components:
    state: StateStore
    service: Service
    runner: JobRunner
    provider: RunPodProvider


def _default_components(config: AppConfig) -> Components:
    state = StateStore(config.data_dir / "runpod_ops.db")
    state.init_schema()
    service = Service(config, state)
    notifier = EmailNotifier(config.notifications.smtp)
    if not config.notifications.smtp.enabled:
        notifier = None
    runner = JobRunner(state, service, notifier=notifier)
    provider = RunPodProvider(config.runpod.api_key_file)
    return Components(state=state, service=service, runner=runner, provider=provider)


def _default_serve(config: AppConfig, host: str, port: int) -> None:
    import uvicorn

    from runpod_ops.web.app import create_app

    uvicorn.run(create_app(config), host=host, port=port)


def _print_logs(state: StateStore, job_id: str, seen: int) -> int:
    for entry in state.get_logs(job_id, after_seq=seen):
        print(entry["line"], flush=True)
        seen = entry["seq"]
    return seen


def _run_mutation(components: Components, profile, action: str) -> int:
    job_id = components.runner.submit(profile, action, "cli")
    seen = 0
    while True:
        seen = _print_logs(components.state, job_id, seen)
        job = components.state.get_job(job_id)
        if job["status"] in {"succeeded", "failed"}:
            return 0 if job["status"] == "succeeded" else 1
        time.sleep(0.2)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runpod-ops")
    parser.add_argument("--config", help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the web UI")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    subparsers.add_parser("pods", help="List configured pods")
    status = subparsers.add_parser("status", help="Show live pod status")
    status.add_argument("name")

    for action in _MUTATIONS:
        sub = subparsers.add_parser(action, help=f"Run {action} for a pod")
        sub.add_argument("name")

    gpus = subparsers.add_parser("gpus", help="List available GPUs")
    gpus.add_argument("--min-mem", type=int)
    gpus.add_argument("--min-vcpu", type=int)
    gpus.add_argument("--max-price", type=float)

    jobs = subparsers.add_parser("jobs", help="List recent jobs")
    jobs.add_argument("--pod")
    return parser


def main(
    argv: list[str] | None = None,
    components_factory: Callable[[AppConfig], Components] = _default_components,
    config_loader: Callable[[str | None], AppConfig] = load_config,
    serve_func: Callable[[AppConfig, str, int], None] = _default_serve,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_loader(args.config)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    components = components_factory(config)

    if args.command == "serve":
        host = args.host or config.server.host
        port = args.port or config.server.port
        serve_func(config, host, port)
        return 0

    if args.command == "pods":
        stored = {row["name"]: row for row in components.state.list_pods()}
        for pod in config.pods:
            row = stored.get(pod.name, {})
            print(f"{pod.name}\t{row.get('pod_id') or '-'}\t{row.get('status') or 'UNKNOWN'}")
        return 0

    if args.command == "jobs":
        for job in components.state.list_jobs(pod=args.pod):
            print(f"{job['id']}\t{job['pod']}\t{job['action']}\t{job['status']}")
        return 0

    try:
        profile = config.get_pod(args.name)
    except KeyError:
        print(f"Unknown pod: {args.name}", file=sys.stderr)
        return 2

    if args.command == "status":
        pod_id = components.service.stored_pod_id(profile)
        if not pod_id:
            print("No pod ID recorded", file=sys.stderr)
            return 1
        try:
            print(json.dumps(components.provider.get_pod(pod_id), indent=2))
        except Exception as exc:
            print(f"Status lookup failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "gpus":
        min_mem = args.min_mem or 80
        min_vcpu = args.min_vcpu or 16
        max_price = args.max_price or 5.0
        for gpu in components.provider.list_gpus(min_mem, min_vcpu, max_price):
            print(
                f"{gpu.get('display_name') or gpu.get('gpu_type')}\t"
                f"{gpu.get('mem_gb')}GB\t{gpu.get('ondemand_price')}"
            )
        return 0

    try:
        return _run_mutation(components, profile, args.command)
    except Exception as exc:
        print(f"Action failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
