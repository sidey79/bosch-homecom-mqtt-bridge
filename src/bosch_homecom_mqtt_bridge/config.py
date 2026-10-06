"""Configuration from environment variables.

Error messages name the offending variable and the allowed values, never the value itself,
so that passwords or credentials embedded in URLs cannot leak through a configuration error.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

BRANDS = ("bosch", "buderus")
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
MQTT_SCHEMES = ("mqtt", "mqtts")
POLL_INTERVAL_MIN = 30
POLL_INTERVAL_MAX = 3600


class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


@dataclass(frozen=True)
class Config:
    mqtt_url: str
    mqtt_username: str | None
    mqtt_password: str | None = field(repr=False)
    mqtt_base_topic: str
    mqtt_client_id: str
    bosch_auth_path: Path
    bosch_device_id: str | None
    bosch_brand: str
    bosch_poll_interval: int
    health_port: int
    log_level: str


def _get(env: Mapping[str, str], name: str) -> str | None:
    """Return the stripped value, treating empty values (``NAME=`` in .env) as unset."""
    value = env.get(name, "").strip()
    return value or None


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer between {low} and {high}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be an integer between {low} and {high}")
    return value


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = (_get(env, name) or default).lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _mqtt_url(env: Mapping[str, str]) -> str:
    value = _get(env, "MQTT_URL") or "mqtt://localhost:1883"
    message = "MQTT_URL must look like mqtt://host[:port] or mqtts://host[:port]"
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise ConfigError(message) from None
    if parts.scheme not in MQTT_SCHEMES or not parts.hostname:
        raise ConfigError(message)
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read and validate the configuration from ``env`` (default: ``os.environ``)."""
    env = os.environ if env is None else env
    return Config(
        mqtt_url=_mqtt_url(env),
        mqtt_username=_get(env, "MQTT_USERNAME"),
        mqtt_password=_get(env, "MQTT_PASSWORD"),
        mqtt_base_topic=_get(env, "MQTT_BASE_TOPIC") or "bosch-homecom",
        mqtt_client_id=_get(env, "MQTT_CLIENT_ID") or "bosch-homecom-mqtt-bridge",
        bosch_auth_path=Path(_get(env, "BOSCH_AUTH_PATH") or "/data/auth.json"),
        bosch_device_id=_get(env, "BOSCH_DEVICE_ID"),
        bosch_brand=_choice(env, "BOSCH_BRAND", "bosch", BRANDS),
        bosch_poll_interval=_int(env, "BOSCH_POLL_INTERVAL", 60, POLL_INTERVAL_MIN, POLL_INTERVAL_MAX),
        health_port=_int(env, "HEALTH_PORT", 8080, 1, 65535),
        log_level=_choice(env, "LOG_LEVEL", "info", LOG_LEVELS),
    )
