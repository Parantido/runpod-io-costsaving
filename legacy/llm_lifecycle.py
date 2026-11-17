#!/usr/bin/env python3
"""
LLM Pod Lifecycle Manager
Creates/terminates RunPod LLM Pods on a weekday schedule and updates Hetzner DNS.
"""

import argparse
import json
import os
import sys
import time
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runpod_costsaving import (
    CloudGPU,
    CloudManager,
    PodInfo,
    read_config_value,
    PortMapping,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'config.sh')
DEFAULT_HETZNER_DNS_API_BASE_URL = 'https://api.hetzner.cloud/v1'


def choose_cheapest_gpu(
    gpus: list,
    min_mem: int,
    min_vcpu: int,
    max_price: float,
) -> CloudGPU:
    """Select cheapest GPU meeting VRAM, vCPU, and price ceiling."""
    qualified = _eligible_gpus(gpus, min_mem, min_vcpu, max_price)
    if not qualified:
        raise RuntimeError(
            f"No eligible GPU: need {min_mem}GB VRAM, {min_vcpu} vCPU, "
            f"max ${max_price:.2f}/hr"
        )
    return qualified[0]


def _eligible_gpus(gpus: list, min_mem: int, min_vcpu: int, max_price: float) -> list:
    """Return eligible GPUs from cheapest to most expensive."""
    qualified = [
        g for g in gpus
        if g.mem_gb >= min_mem
        and g.vcpu >= min_vcpu
        and g.ondemand_price is not None
        and g.ondemand_price <= max_price
    ]
    qualified.sort(key=lambda g: (g.ondemand_price, -g.mem_gb))
    return qualified


def _request(url: str, headers: dict = None, data: bytes = None, method: str = 'GET'):
    """Execute an HTTP request using stdlib urllib."""
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    return urllib.request.urlopen(req)


def _read_dns_json(response, request_url: str) -> dict:
    """Return a DNS API JSON response, rejecting redirects and empty bodies."""
    final_url = response.geturl()
    if isinstance(final_url, str) and final_url != request_url:
        raise RuntimeError(f"Hetzner DNS API redirected {request_url} to {final_url}")
    body = response.read()
    if not body:
        raise RuntimeError(f"Hetzner DNS API returned an empty response for {request_url}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Hetzner DNS API returned invalid JSON for {request_url}: {e}") from e


def runpod_proxy_hostname(pod_id: str, http_port: int) -> str:
    """Return the stable RunPod HTTP proxy hostname for a pod."""
    if not pod_id:
        raise ValueError('RunPod pod ID is empty')
    if not 1 <= int(http_port) <= 65535:
        raise ValueError(f'Invalid RunPod HTTP port: {http_port}')
    return f'{pod_id}-{http_port}.proxy.runpod.net.'


def upsert_hetzner_cname_record(
    token: str,
    zone_name: str,
    fqdn: str,
    target: str,
    ttl: int,
    api_base: str = DEFAULT_HETZNER_DNS_API_BASE_URL,
) -> None:
    """Create or update a Hetzner DNS CNAME record."""
    if not fqdn.endswith(f'.{zone_name}'):
        raise ValueError(f"FQDN {fqdn} is outside zone {zone_name}")
    if not target.endswith('.'):
        raise ValueError('CNAME target must end with a dot')

    api_base = api_base.rstrip('/')
    hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    # Discover zone ID
    url = f'{api_base}/zones?name={urllib.parse.quote(zone_name, safe="")}'
    resp = _request(url, headers=hdrs, method='GET')
    if resp.status >= 300:
        body = resp.read().decode(errors='replace')
        raise RuntimeError(f"Hetzner DNS API error {resp.status}: {body}")
    zones = _read_dns_json(resp, url)
    zone_list = zones.get('zones', [])
    if not zone_list:
        raise RuntimeError(f"Hetzner zone {zone_name} not found")
    zone_id = zone_list[0]['id']
    record_label = fqdn.removesuffix(f'.{zone_name}')

    # Query only the target RRSet, avoiding pagination gaps.
    url = (
        f'{api_base}/zones/{zone_id}/rrsets?'
        f'name={urllib.parse.quote(record_label, safe="")}&type=CNAME'
    )
    resp = _request(url, headers=hdrs, method='GET')
    if resp.status >= 300:
        body = resp.read().decode(errors='replace')
        raise RuntimeError(f"Hetzner DNS API error {resp.status}: {body}")
    rrsets = _read_dns_json(resp, url)
    existing = None
    for r in rrsets.get('rrsets', []):
        if r.get('type') == 'CNAME' and r.get('name') == record_label:
            existing = r
            break

    if existing:
        name = urllib.parse.quote(record_label, safe='')
        url = f'{api_base}/zones/{zone_id}/rrsets/{name}/CNAME/actions/set_records'
        payload = json.dumps({'records': [{'value': target}]}).encode()
        resp = _request(url, headers=hdrs, data=payload, method='POST')
    else:
        payload = json.dumps({
            'type': 'CNAME',
            'name': record_label,
            'ttl': ttl,
            'records': [{'value': target}],
        }).encode()
        resp = _request(f'{api_base}/zones/{zone_id}/rrsets', headers=hdrs, data=payload, method='POST')

    if resp.status >= 300:
        body = resp.read().decode(errors='replace')
        raise RuntimeError(f"Hetzner DNS API error {resp.status}: {body}")


def wait_for_public_ip(
    cloud: CloudManager,
    pod_id: str,
    timeout_seconds: int = 600,
    poll_interval: int = 10,
) -> PodInfo:
    """Poll RunPod until pod is RUNNING."""
    deadline = time.time() + timeout_seconds
    while True:
        info = cloud.get_pod_info_graphql(pod_id)
        if info.status == 'RUNNING':
            return info
        if time.time() >= deadline:
            raise TimeoutError(f"Pod {pod_id} did not become RUNNING")
        time.sleep(poll_interval)


def _load_config() -> dict:
    """Load all LLM and Hetzner config values."""
    keys = {
        'LLM_TEMPLATE_ID': '6qhzwmrnjl',
        'LLM_POD_NAME': 'llm-runpod',
        'LLM_POD_ID_FILE': os.path.join(SCRIPT_DIR, 'logs', 'llm-runpod.pod_id'),
        'LLM_MIN_GPU_MEM': '48',
        'LLM_MIN_VCPU': '16',
        'LLM_MAX_ONDEMAND_PRICE': '1.50',
        'LLM_START_TIMEOUT_SECONDS': '600',
        'LLM_CREATE_TIMEOUT_SECONDS': '1200',
        'LLM_HTTP_PORT': '8080',
        'HETZNER_DNS_ZONE_NAME': 'techfusion.it',
        'HETZNER_DNS_RECORD_NAME': 'runpod.techfusion.it',
        'HETZNER_DNS_TTL': '60',
        'HETZNER_DNS_API_TOKEN': '',
        'HETZNER_DNS_API_BASE_URL': DEFAULT_HETZNER_DNS_API_BASE_URL,
    }
    cfg = {}
    for k, default in keys.items():
        val = read_config_value(k, default)
        if k.startswith('LLM_MIN_') or k.startswith('LLM_MAX_ONDEMAND') or k in ('LLM_START_TIMEOUT_SECONDS', 'LLM_CREATE_TIMEOUT_SECONDS', 'LLM_HTTP_PORT', 'HETZNER_DNS_TTL'):
            cfg[k] = float(val) if '.' in val else int(val)
        else:
            cfg[k] = val
    return cfg


def _pod_id_exists(pod_id_file: str) -> Optional[str]:
    """Return pod ID from file if it exists, else None."""
    if not os.path.exists(pod_id_file):
        return None
    with open(pod_id_file, 'r') as f:
        pid = f.read().strip()
    return pid if pid else None


def _write_pod_id(pod_id_file: str, pod_id: str) -> None:
    """Atomically persist a successfully created pod ID."""
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(pod_id_file), prefix='.llm-pod-')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(pod_id + '\n')
        os.replace(temp_path, pod_id_file)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _is_not_found(error: Exception) -> bool:
    return 'not found' in str(error).lower()


def _update_dns(cfg: dict, pod_id: str) -> str:
    target = runpod_proxy_hostname(pod_id, cfg['LLM_HTTP_PORT'])
    upsert_hetzner_cname_record(
        cfg['HETZNER_DNS_API_TOKEN'],
        cfg['HETZNER_DNS_ZONE_NAME'],
        cfg['HETZNER_DNS_RECORD_NAME'],
        target,
        cfg['HETZNER_DNS_TTL'],
        cfg['HETZNER_DNS_API_BASE_URL'],
    )
    return target


def update_dns(dry_run: bool = False) -> None:
    """Refresh DNS for the tracked, running LLM pod without creating one."""
    cfg = _load_config()
    pod_id = _pod_id_exists(cfg['LLM_POD_ID_FILE'])
    if not pod_id:
        raise RuntimeError('No tracked pod; cannot update DNS')

    cloud = CloudManager()
    info = cloud.get_pod_info_graphql(pod_id)
    if info.status != 'RUNNING':
        raise RuntimeError(f'Pod {pod_id} is not running (status={info.status})')

    target = runpod_proxy_hostname(pod_id, cfg['LLM_HTTP_PORT'])

    if dry_run:
        print(f'[DRY-RUN] Would update DNS: {cfg["HETZNER_DNS_RECORD_NAME"]} -> {target}', file=sys.stderr)
        return

    _update_dns(cfg, pod_id)
    print(f'DNS updated: {cfg["HETZNER_DNS_RECORD_NAME"]} -> {target}', file=sys.stderr)


def start(dry_run: bool = False) -> None:
    """Create a new LLM pod, wait for readiness, update Hetzner DNS."""
    cfg = _load_config()
    cloud = CloudManager()

    # Validate config
    for key in ('LLM_TEMPLATE_ID', 'HETZNER_DNS_API_TOKEN', 'HETZNER_DNS_ZONE_NAME', 'HETZNER_DNS_RECORD_NAME', 'HETZNER_DNS_API_BASE_URL'):
        if not cfg[key]:
            raise RuntimeError(f"Config {key} is empty; check config.sh")

    pod_id_file = cfg['LLM_POD_ID_FILE']
    os.makedirs(os.path.dirname(pod_id_file), exist_ok=True)

    # Check for existing tracked pod
    existing_id = _pod_id_exists(pod_id_file)
    if existing_id:
        print(f"Tracked pod {existing_id} found; checking status...", file=sys.stderr)
        try:
            info = cloud.get_pod_info_graphql(existing_id)
        except Exception as e:
            if not _is_not_found(e):
                raise RuntimeError(f"Pod lookup failed for {existing_id}: {e}") from e
            print(f"Tracked pod {existing_id} is absent; creating a replacement.", file=sys.stderr)
        else:
            if info.status == 'RUNNING':
                target = runpod_proxy_hostname(existing_id, cfg['LLM_HTTP_PORT'])
                print(f"Pod {existing_id} already running; refreshing DNS.", file=sys.stderr)
                if not dry_run:
                    _update_dns(cfg, existing_id)
                    print(f"DNS updated: {cfg['HETZNER_DNS_RECORD_NAME']} -> {target}", file=sys.stderr)
                else:
                    print(f"[DRY-RUN] Would update DNS: {cfg['HETZNER_DNS_RECORD_NAME']} -> {target}", file=sys.stderr)
                return
            print(f"Pod {existing_id} not running (status={info.status}); terminating before fresh creation.", file=sys.stderr)
            cloud.terminate_pod(existing_id)

    min_mem = int(cfg['LLM_MIN_GPU_MEM'])
    min_vcpu = int(cfg['LLM_MIN_VCPU'])
    max_price = float(cfg['LLM_MAX_ONDEMAND_PRICE'])
    search_timeout = int(cfg['LLM_CREATE_TIMEOUT_SECONDS'])
    poll_interval = 15
    errors = []
    new_pod_id = None
    start_time = time.time()
    deadline = start_time + search_timeout
    round_num = 0

    while time.time() < deadline:
        round_num += 1
        print(f"Querying available GPUs... (round {round_num})", file=sys.stderr)
        candidates = _eligible_gpus(cloud.get_available_gpus(), min_mem, min_vcpu, max_price)
        if not candidates:
            errors.append('No eligible GPU reported by RunPod')
            print(f"No eligible GPU right now; polling again in {poll_interval}s "
                  f"({int(time.time() - start_time)}s/{search_timeout}s elapsed).", file=sys.stderr)
            time.sleep(min(poll_interval, max(0, deadline - time.time())))
            continue
        for gpu in candidates:
            print(f"Trying GPU: {gpu.gpu_type} {gpu.mem_gb}GB ${gpu.ondemand_price}/hr", file=sys.stderr)
            if dry_run:
                print(f"[DRY-RUN] Would create pod with template={cfg['LLM_TEMPLATE_ID']}, GPU={gpu.gpu_type}")
                print(f"[DRY-RUN] Would update DNS: {cfg['HETZNER_DNS_RECORD_NAME']} -> <pod-id>-{cfg['LLM_HTTP_PORT']}.proxy.runpod.net.")
                return
            try:
                new_pod_id = cloud.create_pod_graphql(
                    gpu_type_id=gpu.gpu_type,
                    template_id=cfg['LLM_TEMPLATE_ID'],
                    network_volume_id=None,
                    datacenter_id=None,
                    pod_name=cfg['LLM_POD_NAME'],
                    gpu_count=1,
                    container_disk_in_gb=10,
                    volume_in_gb=0,
                    min_memory_in_gb=min_mem,
                    min_vcpu_count=min_vcpu,
                )
                break
            except Exception as e:
                if 'no longer any instances' not in str(e).lower() and 'not available' not in str(e).lower():
                    raise
                errors.append(f'{gpu.gpu_type}: {e}')
                print(f"GPU unavailable: {gpu.gpu_type}; trying next candidate.", file=sys.stderr)
        if new_pod_id:
            break
        print(f"All candidates failed; polling again in {poll_interval}s "
              f"({int(time.time() - start_time)}s/{search_timeout}s elapsed).", file=sys.stderr)
        time.sleep(min(poll_interval, max(0, deadline - time.time())))

    if not new_pod_id:
        elapsed_minutes = max(1, int((time.time() - start_time) / 60))
        raise RuntimeError(
            f'Unable to create an LLM pod within {search_timeout}s after {round_num} rounds '
            f'({elapsed_minutes} min):\n' + '\n'.join(errors[-10:])
        )

    print(f"Pod created: {new_pod_id}", file=sys.stderr)

    _write_pod_id(pod_id_file, new_pod_id)

    # Wait for readiness
    timeout = int(cfg['LLM_START_TIMEOUT_SECONDS'])
    print(f"Waiting up to {timeout}s for pod to become ready...", file=sys.stderr)
    wait_for_public_ip(cloud, new_pod_id, timeout_seconds=timeout)
    target = runpod_proxy_hostname(new_pod_id, cfg['LLM_HTTP_PORT'])
    print(f"Pod {new_pod_id} running; proxy target: {target}", file=sys.stderr)

    # Update DNS
    _update_dns(cfg, new_pod_id)
    print(f"DNS updated: {cfg['HETZNER_DNS_RECORD_NAME']} -> {target}", file=sys.stderr)


def terminate(dry_run: bool = False) -> None:
    """Terminate the tracked LLM pod and remove its ID file."""
    cfg = _load_config()
    pod_id_file = cfg['LLM_POD_ID_FILE']
    pod_id = _pod_id_exists(pod_id_file)

    if not pod_id:
        print("No tracked pod; nothing to terminate.", file=sys.stderr)
        return

    if dry_run:
        print(f"[DRY-RUN] Would terminate pod {pod_id}", file=sys.stderr)
        return

    cloud = CloudManager()
    try:
        cloud.terminate_pod(pod_id)
        print(f"Pod {pod_id} terminated.", file=sys.stderr)
        os.remove(pod_id_file)
        print(f"Removed {pod_id_file}", file=sys.stderr)
    except Exception as e:
        err = str(e)
        if 'not found' in err.lower() or 'Pod not found' in err:
            print(f"Pod {pod_id} already gone; cleaning up ID file.", file=sys.stderr)
            if os.path.exists(pod_id_file):
                os.remove(pod_id_file)
        else:
            raise


def main():
    parser = argparse.ArgumentParser(description='LLM Pod Lifecycle Manager')
    parser.add_argument('command', choices=['start', 'terminate', 'update-dns'], help='Lifecycle command')
    parser.add_argument('--dry-run', action='store_true', help='Simulate without making changes')
    args = parser.parse_args()

    try:
        if args.command == 'start':
            start(dry_run=args.dry_run)
        elif args.command == 'terminate':
            terminate(dry_run=args.dry_run)
        elif args.command == 'update-dns':
            update_dns(dry_run=args.dry_run)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
