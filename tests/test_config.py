import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.config import ConfigError, load_config


def _write(tmp_path, data, name="config.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def _base(tmp_path):
    return {
        "data_dir": str(tmp_path),
        "pods": [
            {
                "name": "riva",
                "template_id": "tpl-1",
                "network_volume_id": "vol-1",
                "min_gpu_mem_gb": 140,
            }
        ],
    }


def test_loads_pods_and_defaults(tmp_path):
    data = _base(tmp_path)
    data["defaults"] = {"min_vcpu": 20, "max_price": 3.5}
    path = _write(tmp_path, data)
    config = load_config(str(path), environ={})
    pod = config.get_pod("riva")
    assert pod.template_id == "tpl-1"
    assert pod.network_volume_id == "vol-1"
    assert pod.min_gpu_mem_gb == 140
    assert pod.min_vcpu == 20
    assert pod.max_price == 3.5


def test_expands_environment_variables(tmp_path):
    path = _write(tmp_path, _base(tmp_path))
    config = load_config(str(path), environ={"RUNPOD_OPS_DATA_DIR": str(tmp_path)})
    assert config.data_dir == tmp_path
    assert config.get_pod("riva").name == "riva"


def test_env_overrides_server_and_notifications(tmp_path):
    path = _write(tmp_path, _base(tmp_path))
    config = load_config(
        str(path),
        environ={
            "RUNPOD_OPS_DATA_DIR": str(tmp_path),
            "RUNPOD_OPS_HOST": "127.0.0.1",
            "RUNPOD_OPS_PORT": "9000",
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "42",
        },
    )
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9000
    assert config.notifications.telegram.enabled is True
    assert config.notifications.telegram.bot_token == "tok"


def test_env_var_expansion_from_yaml(tmp_path):
    data = _base(tmp_path)
    data["pods"][0]["template_id"] = "${MY_TEMPLATE}"
    path = _write(tmp_path, data)
    config = load_config(str(path), environ={"MY_TEMPLATE": "expanded"})
    assert config.get_pod("riva").template_id == "expanded"


def test_duplicate_pod_names_rejected(tmp_path):
    data = _base(tmp_path)
    data["pods"].append(dict(data["pods"][0]))
    path = _write(tmp_path, data)
    with pytest.raises(ConfigError, match="Duplicate pod name"):
        load_config(str(path), environ={})


def test_missing_template_rejected(tmp_path):
    data = {"data_dir": str(tmp_path), "pods": [{"name": "broken"}]}
    path = _write(tmp_path, data)
    with pytest.raises(ConfigError, match="template_id"):
        load_config(str(path), environ={})


def test_explicit_missing_path_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "missing.yaml"), environ={})


def test_get_pod_unknown_raises(tmp_path):
    path = _write(tmp_path, _base(tmp_path))
    config = load_config(str(path), environ={})
    with pytest.raises(KeyError):
        config.get_pod("nope")


def test_secret_generated_and_reused(tmp_path):
    path = _write(tmp_path, _base(tmp_path))
    first = load_config(str(path), environ={})
    second = load_config(str(path), environ={})
    assert first.secret_key
    assert first.secret_key == second.secret_key
    assert (tmp_path / "secret_key").is_file()


def test_secret_env_wins(tmp_path):
    path = _write(tmp_path, _base(tmp_path))
    config = load_config(str(path), environ={"SECRET_KEY": "from-env"})
    assert config.secret_key == "from-env"
