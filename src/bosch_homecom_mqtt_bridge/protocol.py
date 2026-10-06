"""Messages of the MQTT contract (docs/mqtt-contract.md, ADR 0002).

All messages use QoS 1. Status, state and availability are retained; error events are not.
"""
from __future__ import annotations

import enum
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .topics import Topics

QOS = 1
STATUS_STATES = ("starting", "ready", "auth_required", "error", "disconnected")
AVAILABILITY_ONLINE = "online"
AVAILABILITY_OFFLINE = "offline"
UPDATED_AT = "updated_at"
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]*")


class Kind(enum.Enum):
    """Message kind; decides retain and the drop order of the publish queue."""

    STATUS = "status"
    AVAILABILITY = "availability"
    ERROR = "error"
    STATE = "state"


@dataclass(frozen=True)
class Message:
    kind: Kind
    topic: str
    payload: bytes
    retain: bool
    qos: int = QOS


def _json(data: Mapping[str, object]) -> bytes:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def status_message(topics: Topics, state: str, message: str | None = None) -> Message:
    """Retained bridge status; ``connected`` is false only for ``disconnected``."""
    if state not in STATUS_STATES:
        raise ValueError(f"status state must be one of: {', '.join(STATUS_STATES)}")
    data: dict[str, object] = {"state": state, "connected": state != "disconnected"}
    if message:
        data["message"] = message
    return Message(Kind.STATUS, topics.status, _json(data), retain=True)


def will_message(topics: Topics) -> Message:
    """Last will, published by the broker when the bridge drops off without a clean disconnect."""
    return status_message(topics, "disconnected")


def error_message(topics: Topics, code: str, message: str) -> Message:
    """Non-retained error event, e.g. code ``AUTH_REQUIRED``."""
    if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
        raise ValueError("error code must be UPPER_SNAKE_CASE")
    return Message(Kind.ERROR, topics.error, _json({"code": code, "message": message}), retain=False)


def format_timestamp(moment: datetime) -> str:
    """ISO-8601 in UTC with second precision and a ``Z`` suffix."""
    if moment.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_message(
    topics: Topics,
    device_id: str,
    values: Mapping[str, object],
    updated_at: datetime | None = None,
) -> Message:
    """Retained flat JSON of the read values (SI units, no unit text) plus ``updated_at``.

    Non-finite numbers (NaN, ±Inf) have no JSON form and are published as ``null``.
    """
    data: dict[str, object] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError("state keys must be non-empty strings")
        if key == UPDATED_AT:
            raise ValueError(f"state key '{UPDATED_AT}' is reserved")
        if value is not None and not isinstance(value, (bool, int, float, str)):
            raise ValueError(f"state value for '{key}' must be a scalar (flat JSON)")
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        data[key] = value
    data[UPDATED_AT] = format_timestamp(updated_at or datetime.now(timezone.utc))
    return Message(Kind.STATE, topics.state(device_id), _json(data), retain=True)


def availability_message(topics: Topics, device_id: str, online: bool) -> Message:
    """Retained ``online`` or ``offline`` per device."""
    payload = AVAILABILITY_ONLINE if online else AVAILABILITY_OFFLINE
    return Message(Kind.AVAILABILITY, topics.availability(device_id), payload.encode("ascii"), retain=True)
