"""YAML + environment configuration for runpod-ops."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    secure_cookies: bool = False


@dataclass(frozen=True)
class RunPodConfig:
    api_key_file: str = ""


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""

    def __repr__(self) -> str:
        return f"TelegramConfig(enabled={self.enabled})"


@dataclass(frozen=True)
class LokiConfig:
    enabled: bool = False
    url: str = ""


@dataclass(frozen=True)
class SMTPConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    recipient: str = ""
    use_tls: bool = True

    def __repr__(self) -> str:
        return f"SMTPConfig(enabled={self.enabled}, host={self.host!r})"


@dataclass(frozen=True)
class NotificationConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    loki: LokiConfig = field(default_factory=LokiConfig)
    smtp: SMTPConfig = field(default_factory=SMTPConfig)


@dataclass(frozen=True)
class PodProfile:
    name: str
    template_id: str
    network_volume_id: str = ""
    image_name: str = ""
    preferred_gpu: str = ""
    fallback_name: str = ""
    min_gpu_mem_gb: int = 80
    min_vcpu: int = 16
    max_price: float = 4.0
    search_timeout: int = 1200
    poll_interval: int = 15


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    server: ServerConfig = field(default_factory=ServerConfig)
    runpod: RunPodConfig = field(default_factory=RunPodConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    pods: tuple[PodProfile, ...] = ()
    admin_password: str = ""
    secret_key: str = ""
    config_path: str = ""

    def get_pod(self, name: str) -> PodProfile:
        for pod in self.pods:
            if pod.name == name:
                return pod
        raise KeyError(f"Unknown pod: {name}")


def _expand(value: Any, environ: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(item, environ) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, environ) for key, item in value.items()}
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env(environ: dict[str, str], key: str, default: str = "") -> str:
    value = environ.get(key)
    return default if value is None else value


def _resolve_config_path(path: str | None, environ: dict[str, str]) -> str:
    if path:
        if not Path(path).is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    env_path = environ.get("RUNPOD_OPS_CONFIG")
    if env_path:
        if not Path(env_path).is_file():
            raise ConfigError(f"Config file not found: {env_path}")
        return env_path
    for candidate in ("config.yaml", "/data/config.yaml"):
        if Path(candidate).is_file():
            return candidate
    return ""


def _resolve_secret(data_dir: Path, environ: dict[str, str]) -> str:
    secret = environ.get("SECRET_KEY", "").strip()
    if secret:
        return secret
    secret_file = data_dir / "secret_key"
    try:
        if secret_file.is_file():
            existing = secret_file.read_text().strip()
            if existing:
                return existing
    except OSError:
        pass
    generated = secrets.token_hex(32)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(generated)
        secret_file.chmod(0o600)
    except OSError:
        pass
    return generated


def _pod_from_dict(data: dict[str, Any], defaults: dict[str, Any]) -> PodProfile:
    merged = {**defaults, **data}
    name = str(merged.get("name", "")).strip()
    template_id = str(merged.get("template_id", "")).strip()
    if not name:
        raise ConfigError("Pod name is required")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError(f"Pod name {name!r} may only contain letters, digits, dot, dash, underscore")
    if not template_id:
        raise ConfigError(f"Pod {name!r} requires template_id")
    try:
        pod = PodProfile(
            name=name,
            template_id=template_id,
            network_volume_id=str(merged.get("network_volume_id", "") or ""),
            image_name=str(merged.get("image_name", "") or ""),
            preferred_gpu=str(merged.get("preferred_gpu", "") or ""),
            fallback_name=str(merged.get("fallback_name", "") or name),
            min_gpu_mem_gb=int(merged.get("min_gpu_mem_gb", 80)),
            min_vcpu=int(merged.get("min_vcpu", 16)),
            max_price=float(merged.get("max_price", 4.0)),
            search_timeout=int(merged.get("search_timeout", 1200)),
            poll_interval=int(merged.get("poll_interval", 15)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid settings for pod {name!r}: {exc}") from exc
    if pod.min_gpu_mem_gb <= 0 or pod.min_vcpu <= 0:
        raise ConfigError(f"Pod {name!r} requires positive GPU limits")
    if pod.max_price <= 0:
        raise ConfigError(f"Pod {name!r} requires a positive max_price")
    if pod.search_timeout <= 0 or pod.poll_interval <= 0:
        raise ConfigError(f"Pod {name!r} requires positive timeouts")
    return pod


def load_config(
    path: str | None = None, environ: dict[str, str] | None = None
) -> AppConfig:
    env = dict(os.environ if environ is None else environ)
    config_path = _resolve_config_path(path, env)

    raw: dict[str, Any] = {}
    if config_path:
        try:
            loaded = yaml.safe_load(Path(config_path).read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config root must be a mapping: {config_path}")
        raw = _expand(loaded, env)

    data_dir = Path(env.get("RUNPOD_OPS_DATA_DIR") or raw.get("data_dir") or "/data")

    server_raw = raw.get("server") or {}
    server = ServerConfig(
        host=env.get("RUNPOD_OPS_HOST") or server_raw.get("host", "0.0.0.0"),
        port=int(env.get("RUNPOD_OPS_PORT") or server_raw.get("port", 8080)),
        secure_cookies=_as_bool(
            env.get("RUNPOD_OPS_SECURE_COOKIES"), server_raw.get("secure_cookies", False)
        ),
    )

    runpod_raw = raw.get("runpod") or {}
    runpod = RunPodConfig(
        api_key_file=env.get("RUNPOD_API_KEY_FILE")
        or str(runpod_raw.get("api_key_file", "") or "")
    )

    notifications_raw = raw.get("notifications") or {}
    telegram_raw = notifications_raw.get("telegram") or {}
    telegram = TelegramConfig(
        enabled=_as_bool(env.get("TELEGRAM_ENABLED"), telegram_raw.get("enabled", False)),
        bot_token=env.get("TELEGRAM_BOT_TOKEN")
        or str(telegram_raw.get("bot_token", "") or ""),
        chat_id=env.get("TELEGRAM_CHAT_ID") or str(telegram_raw.get("chat_id", "") or ""),
    )
    loki_raw = notifications_raw.get("loki") or {}
    loki = LokiConfig(
        enabled=_as_bool(env.get("LOKI_ENABLED"), loki_raw.get("enabled", False)),
        url=env.get("LOKI_URL") or str(loki_raw.get("url", "") or ""),
    )
    smtp_raw = notifications_raw.get("smtp") or {}
    smtp = SMTPConfig(
        enabled=_as_bool(env.get("SMTP_ENABLED"), smtp_raw.get("enabled", False)),
        host=env.get("SMTP_HOST") or str(smtp_raw.get("host", "") or ""),
        port=int(env.get("SMTP_PORT") or smtp_raw.get("port", 587)),
        username=env.get("SMTP_USERNAME") or str(smtp_raw.get("username", "") or ""),
        password=env.get("SMTP_PASSWORD") or str(smtp_raw.get("password", "") or ""),
        sender=env.get("SMTP_FROM") or str(smtp_raw.get("from", "") or ""),
        recipient=env.get("SMTP_TO") or str(smtp_raw.get("to", "") or ""),
        use_tls=_as_bool(env.get("SMTP_USE_TLS"), smtp_raw.get("use_tls", True)),
    )

    defaults = raw.get("defaults") or {}
    pods_raw = raw.get("pods") or []
    if not isinstance(pods_raw, list):
        raise ConfigError("pods must be a list")
    pods: list[PodProfile] = []
    seen: set[str] = set()
    for item in pods_raw:
        if not isinstance(item, dict):
            raise ConfigError("Each pod entry must be a mapping")
        pod = _pod_from_dict(item, defaults)
        if pod.name in seen:
            raise ConfigError(f"Duplicate pod name: {pod.name}")
        seen.add(pod.name)
        pods.append(pod)

    return AppConfig(
        data_dir=data_dir,
        server=server,
        runpod=runpod,
        notifications=NotificationConfig(telegram=telegram, loki=loki, smtp=smtp),
        pods=tuple(pods),
        admin_password=env.get("ADMIN_PASSWORD", ""),
        secret_key=_resolve_secret(data_dir, env),
        config_path=config_path,
    )
