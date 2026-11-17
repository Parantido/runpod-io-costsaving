import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))


def test_required_packaging_files_exist():
    for name in (
        "Dockerfile",
        "compose.yaml",
        ".env.example",
        "config.example.yaml",
        "README.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "legacy/README.md",
    ):
        assert (ROOT / name).is_file(), name


def test_dockerfile_runs_nonroot_with_healthcheck():
    text = (ROOT / "Dockerfile").read_text()
    assert "HEALTHCHECK" in text
    assert "/api/health" in text
    assert "USER runpod" in text
    assert 'CMD ["runpod-ops", "serve"]' in text
    assert "runpodctl" in text


def test_compose_mounts_state_and_env():
    text = (ROOT / "compose.yaml").read_text()
    assert "./data:/data" in text
    assert "env_file:" in text
    assert ".env" in text
    assert "8080:8080" in text


def test_gitignore_covers_runtime_secrets():
    text = (ROOT / ".gitignore").read_text()
    for entry in (".env", "config.yaml", "data/", ".venv/"):
        assert entry in text


def test_examples_contain_placeholders_only():
    combined = (ROOT / ".env.example").read_text() + (ROOT / "config.example.yaml").read_text()
    for marker in ("techfusion", "drop.co", "danilo.santoro", "parantido"):
        assert marker not in combined.lower()
    assert "change-me" in combined


def test_product_tree_has_no_tenant_identifiers():
    product_files = [
        ROOT / "README.md",
        ROOT / "config.example.yaml",
        ROOT / ".env.example",
        ROOT / "compose.yaml",
        ROOT / "Dockerfile",
    ]
    product_files.extend((ROOT / "runpod_ops").rglob("*.py"))
    product_files.extend((ROOT / "runpod_ops").rglob("*.html"))
    for path in product_files:
        text = path.read_text().lower()
        for marker in ("techfusion", "drop.co", "danilo.santoro", "hetzner", "4f5te8mnxe", "02rh6eeawc"):
            assert marker not in text, f"{marker} leaked into {path}"


def test_legacy_scripts_are_isolated():
    for name in (
        "start.sh",
        "stop.sh",
        "info.sh",
        "update-webservice.sh",
        "test_alerts.sh",
        "dry_run_restart.py",
        "llm_lifecycle.py",
        "config.sh.tpl",
    ):
        assert not (ROOT / name).exists(), name
        assert (ROOT / "legacy" / name).exists(), name
