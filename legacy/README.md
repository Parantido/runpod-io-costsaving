# Legacy scripts

These files are the original single-tenant RunPod automation. They are kept
for reference and migration only; the product lives in `runpod_ops/`.

- `start.sh`, `stop.sh`, `info.sh`: shell orchestration tied to
  `/root/mgmt/runpod` and `config.sh`.
- `update-webservice.sh`: external service updater, excluded from v1.
- `test_alerts.sh`: standalone alert testing.
- `dry_run_restart.py`: restart simulation tool.
- `llm_lifecycle.py`: scheduled LLM pod plus Hetzner DNS, excluded from v1.
- `config.sh.tpl`: old shell configuration template with tenant-specific
  defaults.
- `README.runpod-scripts.md`: the original README.
- `tests/test_llm_lifecycle.py`: tests for the excluded LLM lifecycle.

Use the product CLI and `config.example.yaml` instead. Do not copy secrets or
tenant identifiers from these files into a new deployment.
