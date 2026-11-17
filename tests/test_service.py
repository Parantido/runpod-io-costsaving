import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.config import (
    AppConfig,
    LokiConfig,
    NotificationConfig,
    PodProfile,
    TelegramConfig,
)
from runpod_ops.service import Service, ServiceError
from runpod_ops.state import StateStore


def _config(tmp_path, **pod_kwargs):
    pod = PodProfile(
        name="riva",
        template_id="tpl-1",
        network_volume_id="vol-1",
        image_name="img:1",
        preferred_gpu="NVIDIA H200",
        fallback_name="riva-prod",
        min_gpu_mem_gb=140,
        min_vcpu=20,
        max_price=4.0,
        **pod_kwargs,
    )
    return AppConfig(data_dir=tmp_path, pods=(pod,), secret_key="s")


def _service(tmp_path, **kwargs):
    config = _config(tmp_path, **kwargs)
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    return Service(config, store), store


def test_start_command(tmp_path):
    service, store = _service(tmp_path)
    store.upsert_pod("riva", "pod-1", "STOPPED")
    command = service.build_command(service.config.get_pod("riva"), "start")
    assert command[1:4] == ["-m", "runpod_ops.engine", "start"]
    assert command[4] == "pod-1"
    assert command[-1] == "--json"


def test_stop_and_terminate_commands(tmp_path):
    service, store = _service(tmp_path)
    store.upsert_pod("riva", "pod-1", "RUNNING")
    pod = service.config.get_pod("riva")
    assert service.build_command(pod, "stop")[3:5] == ["stop", "pod-1"]
    assert service.build_command(pod, "terminate")[3:5] == ["remove", "pod-1"]


def test_start_without_pod_id_fails(tmp_path):
    service, _ = _service(tmp_path)
    with pytest.raises(ServiceError, match="pod ID"):
        service.build_command(service.config.get_pod("riva"), "start")


def test_recreate_command_contains_fallback_and_alerts(tmp_path):
    service, _ = _service(tmp_path)
    pod = service.config.get_pod("riva")
    command = service.build_command(pod, "recreate")
    joined = " ".join(command)
    assert "restart-or-recreate" in command
    assert "--fallback-template-id tpl-1" in joined
    assert "--fallback-network-volume-id vol-1" in joined
    assert "--fallback-image-name img:1" in joined
    assert "--fallback-gpu NVIDIA H200" in joined
    assert "--fallback-name riva-prod" in joined
    assert "--max-price 4.0" in joined
    assert "--fallback-min-mem 140" in joined
    assert "--fallback-min-vcpu 20" in joined
    assert "--force-recreate" in command
    assert "--no-telegram" in command
    assert "--no-loki" in command


def test_recreate_passes_alert_credentials(tmp_path):
    pod = PodProfile(name="riva", template_id="tpl-1")
    config = AppConfig(
        data_dir=tmp_path,
        pods=(pod,),
        secret_key="s",
        notifications=NotificationConfig(
            telegram=TelegramConfig(enabled=True, bot_token="tok", chat_id="42"),
            loki=LokiConfig(enabled=True, url="http://loki:3100"),
        ),
    )
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    command = Service(config, store).build_command(pod, "recreate")
    joined = " ".join(command)
    assert "--telegram-token tok" in joined
    assert "--telegram-chat-id 42" in joined
    assert "--loki-url http://loki:3100" in joined
    assert "--no-telegram" not in command


def test_recreate_omits_optional_ids(tmp_path):
    pod = PodProfile(name="bare", template_id="tpl-2")
    config = AppConfig(data_dir=tmp_path, pods=(pod,), secret_key="s")
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    command = Service(config, store).build_command(pod, "recreate")
    assert "--fallback-network-volume-id" not in command
    assert "--fallback-image-name" not in command
    assert "--fallback-gpu" not in command


def test_redaction_masks_secrets(tmp_path):
    service, _ = _service(tmp_path)
    command = ["engine", "--telegram-token", "secret", "--loki-url", "u", "--max-price", "4"]
    redacted = service.redact_command(command)
    assert "secret" not in redacted
    assert "--telegram-token ***" in redacted
    assert "--loki-url u" in redacted


def test_sync_pod_id_from_file(tmp_path):
    service, store = _service(tmp_path)
    pod_file = service.pod_id_file(service.config.get_pod("riva"))
    pod_file.parent.mkdir(parents=True, exist_ok=True)
    pod_file.write_text("pod-99\n")
    assert service.sync_pod_id(service.config.get_pod("riva")) == "pod-99"
    assert store.get_pod("riva")["pod_id"] == "pod-99"


def test_pod_id_file_is_inside_data_dir(tmp_path):
    service, _ = _service(tmp_path)
    path = service.pod_id_file(service.config.get_pod("riva"))
    assert tmp_path in path.parents
    assert path.name == "riva.pod_id"
