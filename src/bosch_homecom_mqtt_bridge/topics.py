"""MQTT topic names of the contract in docs/mqtt-contract.md (ADR 0002).

Every topic segment taken from configuration or from the cloud (base topic, device ID) is validated, so
that a value can never widen a topic into a wildcard, a system topic or another level.
"""
from __future__ import annotations

EVENT_SEGMENT = "event"
_FORBIDDEN_CHARS = ("/", "+", "#", "\x00")


class TopicError(ValueError):
    """Raised for a topic segment that would break the topic structure."""


def validate_segment(segment: str, what: str = "topic segment") -> str:
    """Return ``segment`` if it is usable as exactly one topic level, else raise ``TopicError``.

    Rejected are empty values, ``/``, the wildcards ``+`` and ``#``, NUL and a leading ``$``
    (reserved for broker system topics). The message names the rule, not the value.
    """
    if not isinstance(segment, str) or not segment:
        raise TopicError(f"{what} must be a non-empty string")
    if any(char in segment for char in _FORBIDDEN_CHARS):
        raise TopicError(f"{what} must not contain '/', '+', '#' or NUL")
    if segment.startswith("$"):
        raise TopicError(f"{what} must not start with '$'")
    return segment


def validate_device_id(device_id: str) -> str:
    """A device ID is one topic level and must not collide with the ``event`` level."""
    validate_segment(device_id, "device ID")
    if device_id == EVENT_SEGMENT:
        raise TopicError(f"device ID must not be '{EVENT_SEGMENT}'")
    return device_id


class Topics:
    """Builds the topics below the base topic (``MQTT_BASE_TOPIC``, may span several levels)."""

    def __init__(self, base: str) -> None:
        if not isinstance(base, str) or not base:
            raise TopicError("MQTT_BASE_TOPIC must be a non-empty string")
        for level in base.split("/"):
            validate_segment(level, "MQTT_BASE_TOPIC level")
        self.base = base

    @property
    def status(self) -> str:
        return f"{self.base}/{EVENT_SEGMENT}/status"

    @property
    def error(self) -> str:
        return f"{self.base}/{EVENT_SEGMENT}/error"

    def state(self, device_id: str) -> str:
        return f"{self.base}/{validate_device_id(device_id)}/state"

    def availability(self, device_id: str) -> str:
        return f"{self.base}/{validate_device_id(device_id)}/availability"
