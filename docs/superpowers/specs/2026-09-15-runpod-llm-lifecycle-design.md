# RunPod LLM Lifecycle Design

## Goal

Create a weekday RunPod LLM Pod at 09:00 UTC, permanently terminate it at
17:00 UTC, and update the Hetzner DNS A record for `runpod.techfusion.it` to
the new Pod public IP. Change existing scheduled Riva Pods to recreate at
startup and terminate at shutdown rather than resume and stop.

## Scope

- LLM default template: `6qhzwmrnjl`.
- LLM GPU selection: lowest on-demand price satisfying 48 GB VRAM, 16 vCPU,
  and a configurable price ceiling of $1.50/hr.
- LLM state: `/root/mgmt/runpod/logs/llm-runpod.pod_id`.
- DNS zone: `techfusion.it`; record: `runpod.techfusion.it`; TTL: 60 seconds.
- Existing Riva Pod startup gets opt-in `--force-recreate` behavior.
- Existing Riva scheduled shutdown changes from `stop` to existing `remove`.

## Exclusions

- OpenCode configuration and service reloads.
- DNS record deletion during shutdown.
- A custom HTTPS proxy or stable RunPod port.
- Network volume support for the new LLM lifecycle.

## Architecture

Add a standalone `llm_lifecycle.py` beside the existing manager. It imports
the RunPod GraphQL classes from `runpod_costsaving.py`, but owns only LLM
configuration, GPU price selection, lifecycle state, and Hetzner DNS calls.
It uses only the Python standard library for the DNS client.

`start` is idempotent. A tracked Pod already running is reused and its DNS
record is refreshed. A tracked Pod that is stopped or otherwise not running
is terminated before a fresh Pod is created. New Pods are selected from the
global available-GPU list by price, deployed with the configured template,
then polled until RunPod reports both `RUNNING` and a public IP. The Pod ID is
persisted immediately after creation; this permits a later retry to update
DNS without creating another Pod.

`terminate` is idempotent. A missing ID file or a missing Pod is success. It
uses RunPod GraphQL termination and deletes the state file only after that
succeeds. The DNS record intentionally remains unchanged overnight.

The existing `restart-or-recreate` command gains `--force-recreate`. With
that option it skips `start_pod()` and follows its existing rename, create,
wait, terminate-old-Pod, and state-write path. Without it, all current
behavior remains unchanged.

## Configuration and Secrets

LLM variables are added to `config.sh.tpl` and populated in ignored
`config.sh`. The Hetzner token is `HETZNER_DNS_API_TOKEN` in ignored
`config.sh` only. It must never be placed in code, a cron line, logs, README
examples, or Git.

## Failure Handling

- No eligible GPU: exit nonzero; leave no newly-created state.
- Pod never becomes running with a public IP: exit nonzero; retain its ID for
  inspection and retry.
- DNS failure: exit nonzero; retain its ID so the next `start` refreshes DNS
  without a duplicate Pod.
- Failed termination: exit nonzero; retain its ID.
- Existing forced Riva recreation retains its current backup-Pod behavior.

## Verification

Use `unittest` and mocks for GPU selection, RunPod polling, Hetzner create and
update requests, error handling, state preservation, and termination. Perform
one real `--dry-run start` before cron installation. Do not create a real Pod
or mutate live DNS during automated tests.
