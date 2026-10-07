"""Discovery and poll loop with the bridge status machine (MQTT contract).

``TokenManager.run_poll`` does ``ensure_fresh()`` first and applies the poll timeout only to the
request itself, never to a refresh. The interval counts from the end of a poll. Error events and logs
carry the exception type only: ``repr(exc)``, ``request_info`` and ``ClientResponseError`` can contain
the Authorization header.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

from tenacity import RetryError

from .adapters import Adapter, adapter_for
from .auth.token_manager import AUTH_REQUIRED, DISCONNECTED, TokenManager
from .config import Config
from .publisher import MqttPublisher
from .topics import TopicError, validate_device_id

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T")

POLL_FAILED = "POLL_FAILED"


def error_name(error: BaseException) -> str:
    """Type name of the failure; a tenacity ``RetryError`` is unwrapped to its cause."""
    if isinstance(error, RetryError):
        cause = error.last_attempt.exception()
        if cause is not None:
            return type(cause).__name__
    return type(error).__name__


def error_cause(error: BaseException) -> str | None:
    """Underlying cause for the log: ``HTTP <status>`` or the type name, never a message or headers.

    ``homecom_alt`` wraps timeouts and HTTP 429 alike in ``NotRespondingError``; the cause tells them apart.
    """
    if isinstance(error, RetryError):
        error = error.last_attempt.exception() or error
    cause = error.__cause__
    if cause is None:
        return None
    status = getattr(cause, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"HTTP {status}"
    return type(cause).__name__


class Poller:
    def __init__(self, config: Config, publisher: MqttPublisher, interval: float | None = None) -> None:
        self._config = config
        self._publisher = publisher
        self._interval = config.bosch_poll_interval if interval is None else interval
        self.status = "starting"  # last status published (starting, ready, auth_required, error)
        self._message: str | None = None
        self._devices: dict[str, tuple[Adapter, Any]] = {}
        self._online: dict[str, bool] = {}
        self._discovered = False
        self._unknown_types: set[str] = set()

    def set_status(self, state: str, message: str | None = None) -> None:
        if (state, message) != (self.status, self._message):
            self.status, self._message = state, message
            self._publisher.publish_status(state, message)

    def on_token_state(self, state: str) -> None:
        """Token manager state: ``auth_required`` and cloud unreachable (K3/K4) take the devices offline."""
        if state == AUTH_REQUIRED:
            self.set_status("auth_required", "login required")
        elif state == DISCONNECTED:
            self.set_status("error", "cloud unreachable")
        else:
            return
        self._all_offline()

    def _set_online(self, device_id: str, online: bool) -> None:
        if self._online.get(device_id) is not online:
            self._online[device_id] = online
            self._publisher.publish_availability(device_id, online)

    def _all_offline(self) -> None:
        for device_id in self._devices:
            self._set_online(device_id, False)

    async def run(self, tokens: TokenManager, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set."""
        while not stop.is_set():
            await self._until_stop(self._cycle(tokens), stop)
            if stop.is_set():
                return
            await self._until_stop(asyncio.sleep(self._interval), stop)

    @staticmethod
    async def _until_stop(awaitable: Awaitable[T], stop: asyncio.Event) -> T | None:
        """Result of ``awaitable``, or None if ``stop`` came first (the work is cancelled)."""
        work = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            return None
        finally:
            for task in (work, waiter):
                task.cancel()
            await asyncio.gather(work, waiter, return_exceptions=True)

    async def _cycle(self, tokens: TokenManager) -> None:
        try:
            if not self._discovered:
                await self._discover(tokens)
            await self._poll(tokens)
        except Exception as error:  # noqa: BLE001 - classified by type name only
            self._failed(error)

    def _failed(self, error: BaseException) -> None:
        name = error_name(error)
        cause = error_cause(error)
        _LOGGER.warning("Cloud request failed: %s%s", name, f" (cause: {cause})" if cause else "")
        if self.status == "auth_required":
            return  # the token manager already reported it; a login ends the state
        if self.status != "error":
            self._publisher.publish_error(POLL_FAILED, f"cloud request failed ({name})")
        self.set_status("error", f"cloud request failed ({name})")
        self._all_offline()

    async def _discover(self, tokens: TokenManager) -> None:
        api = tokens.fetch_api()

        async def devices() -> object:
            found = await api.async_get_devices()
            return await found if inspect.isawaitable(found) else found

        found = await tokens.run_poll(devices)
        if not isinstance(found, list):
            raise TypeError("unexpected discovery response")
        for entry in found:
            entry = entry if isinstance(entry, dict) else {}
            device_id, device_type = entry.get("deviceId"), entry.get("deviceType")
            if self._config.bosch_device_id and device_id != self._config.bosch_device_id:
                continue
            adapter = adapter_for(device_type)
            if adapter is None:
                label = device_type if isinstance(device_type, str) and device_type.isprintable() else "?"
                if label not in self._unknown_types:
                    self._unknown_types.add(label)
                    _LOGGER.info("Ignoring device type %s: no adapter", label[:64])
                continue
            try:
                validate_device_id(device_id)  # type: ignore[arg-type]
            except (TopicError, TypeError):
                _LOGGER.warning("Ignoring a %s device with an unusable device id", adapter.device_type)
                continue
            self._devices[device_id] = (adapter, adapter.create_api(tokens, device_id))
        self._discovered = True
        _LOGGER.info("Discovered %d supported device(s)", len(self._devices))

    async def _poll(self, tokens: TokenManager) -> None:
        if not self._devices:
            self.set_status("ready")  # cloud reachable, nothing supported to read
            return
        ok = 0
        failure: BaseException | None = None
        for device_id, (adapter, api) in self._devices.items():
            try:
                values = await tokens.run_poll(lambda a=adapter, c=api, d=device_id: a.read(c, d))
            except Exception as error:  # noqa: BLE001 - classified by type name only
                failure = error
                self._set_online(device_id, False)
                continue
            self._publisher.publish_state(device_id, values)
            self._set_online(device_id, True)
            ok += 1
        if ok:
            self.set_status("ready")
        elif failure is not None:
            raise failure
