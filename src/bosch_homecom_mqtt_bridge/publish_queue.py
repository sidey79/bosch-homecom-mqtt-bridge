"""Bounded publish queue with the overflow rule of the MQTT contract (ADR 0002).

Status and availability messages are retained, so only the newest one per topic matters: a new one
replaces the queued one of the same topic (it moves to the end). They are never dropped otherwise, so
the queue can exceed its limit by at most one message per status or availability topic.

When the queue is full, the oldest state message is dropped first, error events only when no state
message is left. The incoming message takes part in the choice as the newest of its kind, so a new
state message is dropped when the queue holds only errors and protected messages.
"""
from __future__ import annotations

from collections import deque

from .protocol import Kind, Message

DROP_ORDER = (Kind.STATE, Kind.ERROR)
COALESCED = (Kind.STATUS, Kind.AVAILABILITY)  # retained: only the newest message per topic is kept
DEFAULT_MAXSIZE = 1000


class PublishQueue:
    def __init__(self, maxsize: int = DEFAULT_MAXSIZE) -> None:
        if maxsize < 1:
            raise ValueError("queue size must be at least 1")
        self.maxsize = maxsize
        self._items: deque[Message] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def _queued_same_topic(self, message: Message) -> int | None:
        if message.kind in COALESCED:
            for index, queued in enumerate(self._items):
                if queued.kind is message.kind and queued.topic == message.topic:
                    return index
        return None

    def put(self, message: Message) -> Message | None:
        """Append ``message`` and return the message dropped for it, if any.

        A replaced older status or availability message of the same topic is not reported as dropped.
        """
        index = self._queued_same_topic(message)
        if index is not None:
            del self._items[index]
            self._items.append(message)
            return None
        if len(self._items) < self.maxsize:
            self._items.append(message)
            return None
        for kind in DROP_ORDER:
            for index, queued in enumerate(self._items):
                if queued.kind is kind:
                    del self._items[index]
                    self._items.append(message)
                    return queued
            if message.kind is kind:
                return message
        self._items.append(message)
        return None

    def push_front(self, message: Message) -> None:
        """Put a message back at the head (retry or status replay); never drops a queued message.

        If a status or availability message of the same topic is already queued, that one is at least as
        new, so ``message`` is not put back.
        """
        if self._queued_same_topic(message) is None:
            self._items.appendleft(message)

    def pop(self) -> Message:
        """Remove and return the oldest message."""
        return self._items.popleft()

    def snapshot(self) -> list[Message]:
        return list(self._items)
