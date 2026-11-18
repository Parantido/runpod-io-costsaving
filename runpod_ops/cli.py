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
from runpod_ops.scheduler import ScheduleError, ScheduleExecutor, validate_cron
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

    uvicorn.run(
        create_app(config, start_scheduler=True), host=host, port=port
    )


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


def _print_job_output(state: StateStore, job_ids: list[str]) -> None:
    for job_id in job_ids:
        for entry in state.get_logs(job_id):
            print(entry["line"], flush=True)


def _validate_schedule(
    config: AppConfig, pod: str, action: str, cron: str, timezone_name: str
) -> None:
    config.get_pod(pod)
    if action not in _MUTATIONS:
        raise ScheduleError(f"Unsupported action: {action}")
    validate_cron(cron, timezone_name)


def _schedule_command(
    config: AppConfig, components: Components, args
) -> int:
    state = components.state
    command = args.schedule_command

    if command == "add":
        try:
            _validate_schedule(
                config, args.pod, args.action, args.cron, args.timezone
            )
            state.create_schedule(
                args.name,
                args.pod,
                args.action,
                args.cron,
                args.timezone,
                args.trigger,
                args.enable,
                args.pre_hook or (),
                args.post_hook or (),
            )
        except Exception as exc:
            print(f"Schedule add failed: {exc}", file=sys.stderr)
            return 2
        print(f"Created schedule {args.name}")
        return 0

    try:
        schedule = state.get_schedule_by_name(args.name)
    except Exception:
        schedule = None
    if not schedule:
        print(f"Unknown schedule: {args.name}", file=sys.stderr)
        return 2

    if command == "delete":
        state.delete_schedule(schedule["id"])
        print(f"Deleted schedule {args.name}")
        return 0

    if command in {"enable", "disable"}:
        state.update_schedule(schedule["id"], enabled=command == "enable")
        print(f"Schedule {args.name} {command}d")
        return 0

    if command == "run":
        executor = ScheduleExecutor(state, config, components.runner)
        result = executor.run(schedule, actor="cli")
        _print_job_output(state, result.get("job_ids", []))
        print(f"Schedule {args.name}: {result.get('status')}")
        return 0 if result.get("status") == "succeeded" else 1

    changes: dict = {}
    if args.pod:
        try:
            config.get_pod(args.pod)
        except KeyError as exc:
            print(f"Unknown pod: {args.pod}", file=sys.stderr)
            return 2
        changes["pod"] = args.pod
    if args.action:
        changes["action"] = args.action
    if args.cron is not None:
        changes["cron"] = args.cron
    if args.timezone:
        changes["timezone"] = args.timezone
    if args.trigger:
        changes["trigger"] = args.trigger
    if args.enable and args.disable:
        print("Choose --enable or --disable", file=sys.stderr)
        return 2
    if args.enable:
        changes["enabled"] = True
    if args.disable:
        changes["enabled"] = False
    if args.pre_hook is not None:
        changes["pre_hooks"] = args.pre_hook
    if args.post_hook is not None:
        changes["post_hooks"] = args.post_hook

    pod = changes.get("pod", schedule["pod"])
    action = changes.get("action", schedule["action"])
    cron = changes.get("cron", schedule["cron"])
    timezone_name = changes.get("timezone", schedule["timezone"])
    try:
        _validate_schedule(config, pod, action, cron, timezone_name)
    except Exception as exc:
        print(f"Schedule edit failed: {exc}", file=sys.stderr)
        return 2
    state.update_schedule(schedule["id"], **changes)
    print(f"Updated schedule {args.name}")
    return 0


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

    subparsers.add_parser("schedules", help="List schedules")
    adopt = subparsers.add_parser("adopt", help="Attach an existing pod ID")
    adopt.add_argument("name")
    adopt.add_argument("pod_id")

    schedule = subparsers.add_parser("schedule", help="Manage schedules")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_sub.add_parser("list", help="List schedules")

    add = schedule_sub.add_parser("add", help="Create a schedule")
    add.add_argument("name")
    add.add_argument("--pod", required=True)
    add.add_argument("--action", required=True, choices=_MUTATIONS)
    add.add_argument("--cron", required=True)
    add.add_argument("--timezone", default="Etc/UTC")
    add.add_argument("--trigger", choices=("external", "app"), default="external")
    add.add_argument("--enable", action="store_true")
    add.add_argument("--pre-hook", action="append")
    add.add_argument("--post-hook", action="append")

    edit = schedule_sub.add_parser("edit", help="Edit a schedule")
    edit.add_argument("name")
    edit.add_argument("--pod")
    edit.add_argument("--action", choices=_MUTATIONS)
    edit.add_argument("--cron")
    edit.add_argument("--timezone")
    edit.add_argument("--trigger", choices=("external", "app"))
    edit.add_argument("--enable", action="store_true")
    edit.add_argument("--disable", action="store_true")
    edit.add_argument("--pre-hook", action="append")
    edit.add_argument("--post-hook", action="append")

    for command in ("enable", "disable", "delete", "run"):
        sub = schedule_sub.add_parser(command, help=f"{command} a schedule")
        sub.add_argument("name")
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

    if args.command == "adopt":
        try:
            profile = config.get_pod(args.name)
        except KeyError:
            print(f"Unknown pod: {args.name}", file=sys.stderr)
            return 2
        try:
            components.service.adopt(profile, args.pod_id)
        except Exception as exc:
            print(f"Adopt failed: {exc}", file=sys.stderr)
            return 1
        print(f"Adopted {args.name} as {args.pod_id}")
        return 0

    if args.command == "schedules" or (
        args.command == "schedule" and args.schedule_command == "list"
    ):
        for item in components.state.list_schedules():
            print(
                f"{item['name']}\t{item['pod']}\t{item['action']}\t"
                f"{item['cron']}\t{item['timezone']}\t{item['trigger']}\t"
                f"{'enabled' if item['enabled'] else 'disabled'}\t"
                f"{item['last_status'] or '-'}\t{item['next_run_at'] or '-'}"
            )
        return 0

    if args.command == "schedule":
        return _schedule_command(config, components, args)

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
