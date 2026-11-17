import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.engine import CloudManager, read_config_value


def test_read_config_value_env_wins(monkeypatch):
    monkeypatch.setenv("FOO_SETTING", "env")
    assert read_config_value("FOO_SETTING", "default") == "env"


def test_read_config_value_default_when_absent(monkeypatch):
    monkeypatch.delenv("FOO_SETTING", raising=False)
    assert read_config_value("FOO_SETTING", "default") == "default"


def test_api_key_prefers_env(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "secret")
    assert CloudManager()._get_api_key() == "secret"


def test_api_key_file_env(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("file-secret\n")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setenv("RUNPOD_API_KEY_FILE", str(key_file))
    assert CloudManager()._get_api_key() == "file-secret"
