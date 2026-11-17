# RunPod LLM Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate fresh weekday RunPod LLM Pod creation and termination, publish its public IP through Hetzner DNS, and make existing scheduled Pods recreate instead of resume.

**Architecture:** Keep `runpod_costsaving.py` as the shared RunPod API layer. Add the smallest opt-in branch, `--force-recreate`, to its existing `restart-or-recreate` command. Add a focused `llm_lifecycle.py` that imports the existing RunPod classes, selects the cheapest qualifying GPU, persists one Pod ID, and upserts one Hetzner DNS A record with `urllib`.

**Tech Stack:** Python 3 standard library, existing RunPod GraphQL/runpodctl integration, root crontab, Hetzner DNS HTTP API, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-15-runpod-llm-lifecycle-design.md`

## Global Constraints

- Do not add third-party Python packages.
- Keep default `restart-or-recreate` behavior unchanged when `--force-recreate` is absent.
- LLM defaults: template `6qhzwmrnjl`, 48 GB GPU VRAM, 16 vCPU, $1.50/hr maximum, 600-second readiness timeout.
- LLM uses no network volume.
- Hetzner token exists only in ignored `config.sh`; never log it.
- Update DNS only after RunPod returns `RUNNING` and a public IPv4 address.
- Do not modify OpenCode configuration, systemd services, or existing cron schedules other than the specified RunPod lifecycle commands.

---

## File Structure

- `runpod_costsaving.py`: existing RunPod API/CLI; receives one opt-in forced-recreate flag.
- `llm_lifecycle.py`: new LLM-only orchestration, state, GPU selection, and Hetzner DNS client.
- `config.sh.tpl`: checked-in configuration template with non-secret LLM defaults and blank token value.
- `config.sh`: ignored live configuration populated by operator, including DNS token.
- `tests/test_llm_lifecycle.py`: standard-library unit tests for new LLM module.
- `README.md`: operational configuration, dry-run, and cron documentation.
- root crontab: schedules the LLM lifecycle and changes Riva shutdown to permanent removal.

### Task 1: Add Forced Recreation Flag

**Files:**
- Modify: `runpod_costsaving.py:1912-1942`
- Modify: `runpod_costsaving.py:2309-2326`

**Interfaces:**
- Consumes: `restart-or-recreate` parser and existing `pod_exists`, `pod_mgr` variables.
- Produces: `args.force_recreate: bool`; when true, code follows the existing recreation path rather than calling `RunPodManager.start_pod()`.

- [ ] **Step 1: Add parser option**

Add exactly after `--try-without-network-volume`:

```python
restart_parser.add_argument(
    '--force-recreate',
    action='store_true',
    help='Skip restarting an existing pod and create a replacement instead',
)
```

- [ ] **Step 2: Gate only the restart attempt**

Change this condition:

```python
if pod_exists and pod_mgr:
```

to:

```python
if pod_exists and pod_mgr and not args.force_recreate:
```

Do not alter code below `if restart_success:`. With the flag, execution enters
the existing recreation branch: it renames the old Pod, deploys a replacement,
waits for `RUNNING`, terminates the backup Pod, and writes the new ID.

- [ ] **Step 3: Compile-check the change**

Run:

```bash
python3 -m py_compile runpod_costsaving.py
```

Expected: no output and exit code zero.

- [ ] **Step 4: Commit isolated change**

```bash
git add runpod_costsaving.py
```

### Task 2: Add LLM Configuration

**Files:**
- Modify: `config.sh.tpl` after line 42
- Modify: ignored `config.sh` after its existing configuration

**Interfaces:**
- Produces: configuration keys consumed by `llm_lifecycle.py` through `read_config_value()`.

- [ ] **Step 1: Add template settings**

Append this exact block to `config.sh.tpl`:

```bash
# LLM lifecycle configuration
LLM_TEMPLATE_ID="6qhzwmrnjl"
LLM_POD_NAME="llm-runpod"
LLM_POD_ID_FILE="/root/mgmt/runpod/logs/llm-runpod.pod_id"
LLM_MIN_GPU_MEM=48
LLM_MIN_VCPU=16
LLM_MAX_ONDEMAND_PRICE=1.50
LLM_START_TIMEOUT_SECONDS=600
HETZNER_DNS_ZONE_NAME="techfusion.it"
HETZNER_DNS_RECORD_NAME="runpod.techfusion.it"
HETZNER_DNS_TTL=60
HETZNER_DNS_API_TOKEN=""
```

- [ ] **Step 2: Populate live ignored config**

Copy the block to `config.sh`. Replace only:

```bash
HETZNER_DNS_API_TOKEN="<operator-supplied-token>"
```

Never add the actual token to `config.sh.tpl`, Git, shell history, or a cron
entry. Set restrictive permissions:

```bash
chmod 600 config.sh
```

- [ ] **Step 3: Commit checked-in template only**

Verify ignored secrets are not staged:

```bash
git status --short
```

Expected: `config.sh` is absent from staged files.

### Task 3: Create Pure LLM Selection and DNS Helpers

**Files:**
- Create: `llm_lifecycle.py`
- Create: `tests/test_llm_lifecycle.py`

**Interfaces:**
- Consumes: `CloudGPU` from `runpod_costsaving.py`.
- Produces:

```python
def choose_cheapest_gpu(
    gpus: list[CloudGPU], min_mem: int, min_vcpu: int, max_price: float
) -> CloudGPU: ...

def upsert_hetzner_a_record(
    token: str, zone_name: str, fqdn: str, ip: str, ttl: int
) -> None: ...
```

- [ ] **Step 1: Write GPU selection tests**

Create tests with synthetic `CloudGPU` values. Test that a 48 GB `$0.70/hr`
GPU beats a 48 GB `$0.90/hr` GPU; a 40 GB GPU is excluded; a 48 GB GPU over
the ceiling is excluded; a GPU with `ondemand_price=None` is excluded; and
zero eligible GPUs raises `RuntimeError` containing `No eligible GPU`.

- [ ] **Step 2: Run selection tests and confirm failure**

```bash
python3 -m unittest -v tests.test_llm_lifecycle
```

Expected: import failure because `llm_lifecycle.py` does not exist.

- [ ] **Step 3: Implement `choose_cheapest_gpu`**

Import `CloudGPU`; filter with these exact conditions:

```python
gpu.mem_gb >= min_mem
```

Sort qualifying GPUs with:

```python
key=lambda gpu: (gpu.ondemand_price, -gpu.mem_gb)
```

Raise:

```python
raise RuntimeError(
    f"No eligible GPU: need {min_mem}GB VRAM, {min_vcpu} vCPU, "
    f"max ${max_price:.2f}/hr"
)
```

- [ ] **Step 4: Write mocked Hetzner request tests**

Patch a small module-local HTTP request helper. Test:

```text
GET /zones?name=techfusion.it
GET /records?zone_id=zone-id
PUT /records/record-id
```

when an A record named `runpod` exists, and test `POST /records` when it does
not. Assert the JSON payload has `zone_id`, `type: A`, `name: runpod`, the
new IPv4 value, and `ttl: 60`.

- [ ] **Step 5: Implement DNS helper**

Use `urllib.request`; set headers:

```python
{
    "Auth-API-Token": token,
    "Content-Type": "application/json",
}
```

Derive the record label by removing `.{zone_name}` from `fqdn`. Reject an FQDN
outside the zone and reject input failing `ipaddress.IPv4Address(ip)`. For any
non-2xx response, raise `RuntimeError` containing status and response body,
but never request headers or token.

- [ ] **Step 6: Run helper tests**

```bash
python3 -m unittest -v tests.test_llm_lifecycle
```

Expected: all Task 3 tests pass.

- [ ] **Step 7: Commit helper layer**

```bash
git add llm_lifecycle.py tests/test_llm_lifecycle.py
```

### Task 4: Implement LLM Start and Terminate

**Files:**
- Modify: `llm_lifecycle.py`
- Modify: `tests/test_llm_lifecycle.py`

**Interfaces:**
- Consumes: `CloudManager`, `RunPodManager`, `read_config_value`,
  `choose_cheapest_gpu`, `upsert_hetzner_a_record`.
- Produces:

```python
def wait_for_public_ip(pod_id: str, timeout_seconds: int) -> PodInfo: ...
def start() -> None: ...
def terminate() -> None: ...
def main() -> None: ...
```

- [ ] **Step 1: Write polling tests**

Mock `CloudManager.get_pod_info_graphql()` to return, in order:

1. `STARTING` without IP.
2. `RUNNING` without IP.
3. `RUNNING` with `203.0.113.10`.

Assert `wait_for_public_ip()` returns only the third object. Add a timeout
test that raises `TimeoutError` containing the Pod ID.

- [ ] **Step 2: Implement readiness polling**

Poll every 10 seconds. Return only where:

```python
pod_info.status == "RUNNING" and pod_info.public_ip
```

After `timeout_seconds`, raise:

```python
TimeoutError(f"Pod {pod_id} did not become RUNNING with a public IP")
```

- [ ] **Step 3: Write start behavior tests**

Mock `CloudManager`, filesystem state, `wait_for_public_ip`, and DNS helper.
Cover:

- No state file: select GPU, call `create_pod_graphql()` with
  `network_volume_id=None` and `datacenter_id=None`, write ID, then update DNS.
- Running tracked Pod: do not create another Pod; poll then update DNS.
- Non-running tracked Pod: call `terminate_pod(old_id)`, then create new Pod.
- DNS failure: state file still holds new Pod ID.

- [ ] **Step 4: Implement `start`**

Read each `LLM_*` and `HETZNER_*` value using existing `read_config_value`.
Require nonempty template, token, zone, record, and Pod ID path. If an ID file
exists, call `CloudManager.get_pod_info_graphql()`. Treat a lookup failure as
an absent Pod. If status is not `RUNNING`, call `terminate_pod()` before
creation. For new Pods call:

```python
cloud.create_pod_graphql(
    gpu_type_id=gpu.gpu_type,
    template_id=template_id,
    network_volume_id=None,
    datacenter_id=None,
    pod_name=pod_name,
    gpu_count=1,
    min_vcpu_count=min_vcpu,
)
```

Write the new ID with a trailing newline before waiting for readiness. Call
DNS only after readiness succeeds.

- [ ] **Step 5: Write termination tests**

Cover missing ID file as success, successful `terminate_pod()` removing the
file, and `terminate_pod()` failure retaining the file. Confirm DNS helper is
never called by `terminate`.

- [ ] **Step 6: Implement `terminate` and CLI**

Use `argparse` with required positional command choices `start` and
`terminate`, plus `--dry-run`. Missing state file returns zero. A `Pod not
found` error returns zero; other termination errors raise. Delete state only
after success. In `--dry-run start`, print selected GPU and planned DNS record
without calling create, terminate, or DNS helpers.

- [ ] **Step 7: Run complete test and syntax suite**

```bash
python3 -m unittest -v tests.test_llm_lifecycle
python3 -m py_compile llm_lifecycle.py runpod_costsaving.py
```

Expected: all tests pass, both files compile.

- [ ] **Step 8: Commit lifecycle implementation**

```bash
git add llm_lifecycle.py tests/test_llm_lifecycle.py
```

### Task 5: Install Cron Jobs and Document Operations

**Files:**
- Modify: root crontab
- Modify: `README.md`

**Interfaces:**
- Consumes: `llm_lifecycle.py start`, `llm_lifecycle.py terminate`, and
  existing `restart-or-recreate --force-recreate` / `remove` commands.

- [ ] **Step 1: Add LLM weekday cron entries**

Add exactly these root crontab entries:

```cron
0 9 * * 1-5 /root/mgmt/runpod/llm_lifecycle.py start >> /root/mgmt/runpod/logs/llm-lifecycle.log 2>&1
0 17 * * 1-5 /root/mgmt/runpod/llm_lifecycle.py terminate >> /root/mgmt/runpod/logs/llm-lifecycle.log 2>&1
```

These are UTC because root crontab has no `CRON_TZ` setting. Do not add one.

- [ ] **Step 2: Update Riva startup cron commands**

For each existing `restart-or-recreate` root cron command for
`tr-gpu-01-prod` and `tr-gpu-02-prod`, append:

```text
--force-recreate
```

Do not alter templates, volume IDs, GPU limits, start times, or follow-up web
service commands.

- [ ] **Step 3: Update Riva shutdown cron commands**

Replace only the action word `stop` with `remove` in these existing lines:

```cron
/root/mgmt/runpod/runpod_costsaving.py --json --pod-id-file /root/mgmt/runpod/logs/tr-gpu-01-prod.pod_id remove
/root/mgmt/runpod/runpod_costsaving.py --json --pod-id-file /root/mgmt/runpod/logs/tr-gpu-02-prod.pod_id remove
```

`remove` already calls `runpodctl remove pod`, which permanently terminates
the Pod. Do not change the script's global `stop` action.

- [ ] **Step 4: Document configuration and operational behavior**

Add a concise README section containing:

- LLM config keys and default values, excluding token value.
- `start`, `terminate`, and `--dry-run start` commands.
- Weekday UTC schedule.
- DNS update occurs only after a public IP exists.
- A record exposes IP only; RunPod public port remains separate.
- Existing Riva crons now recreate on startup and remove on shutdown.

- [ ] **Step 5: Verify cron and dry-run**

Run:

```bash
crontab -l
/root/mgmt/runpod/llm_lifecycle.py --dry-run start
```

Expected: exact 09:00/17:00 weekday LLM entries; dry-run does not create,
terminate, or update DNS.

- [ ] **Step 6: Run final verification**

Run:

```bash
python3 -m unittest -v tests.test_llm_lifecycle
python3 -m py_compile llm_lifecycle.py runpod_costsaving.py
git status --short
git diff --check
```

Expected: tests and compilation pass; no token, Pod ID file, log, or live
`config.sh` is staged; no whitespace errors.

- [ ] **Step 7: Commit docs and tracked changes**

```bash
git add README.md config.sh.tpl runpod_costsaving.py llm_lifecycle.py tests
```
