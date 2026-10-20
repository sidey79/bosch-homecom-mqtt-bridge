"""The ``run`` command: wires publisher, token manager, poller and health server, and shuts down in order.

Shutdown (ADR 0001): SIGTERM/SIGINT set a stop event. In the ``finally`` of ``run`` the token manager
is closed first, together with the MQTT login (a running refresh POST or code exchange and its ``auth.json``
write finish), then the ``ClientSession``,
then the MQTT publisher (final status, broker ack, at most 9 s). All of it stays within the container's
``stop_grace_period`` of 20 s; the publisher timeout is shortened if the refresh used up the time.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from aiohttp import ClientSession

from .auth.store import AuthStore
from .auth.token_manager import TokenManager
from .config import Config
from .health import HealthServer
from .mqtt_login import MqttLogin
from .poller import Poller
from .publisher import STOP_TIMEOUT, MqttPublisher

_LOGGER = logging.getLogger(__name__)
SHUTDOWN_BUDGET = 18.0  # seconds, below stop_grace_period (20 s)


def readiness(status: str, broker_connected: bool) -> str:
    """Status shown by /readyz: a lost broker connection always reads as disconnected."""
    return status if broker_connected else "disconnected"


async def run(config: Config, stop: asyncio.Event | None = None) -> int:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    publisher = MqttPublisher(config)
    poller = Poller(config, publisher)
    health = HealthServer(config.health_port, lambda: readiness(poller.status, publisher.connected))
    session = ClientSession()
    store = AuthStore(config.bosch_auth_path)  # shared, so its asyncio lock orders login and refresh
    login = MqttLogin(store, config.bosch_brand, session, publisher) if config.mqtt_login_enabled else None
    tokens = TokenManager(
        store,
        session,
        brand=config.bosch_brand,
        poll_timeout=config.bosch_poll_timeout,
        on_state=poller.on_token_state,
        on_error=publisher.publish_error,
    )
    try:
        if login is not None:
            login.start()  # registers the subscription before the first connect
        await publisher.start()
        publisher.publish_status("starting")
        await health.start()
        _LOGGER.info("Bridge started")
        await poller.run(tokens, stop)
    finally:
        stopped_at = time.monotonic()
        try:
            # Side by side, so a refresh waiting behind the login's store lock is cancelled at once and
            # the two 15 s request timeouts do not add up.
            results = await asyncio.gather(
                *([login.aclose()] if login is not None else []), tokens.aclose(), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            try:
                await session.close()
            finally:
                try:
                    await health.stop()
                finally:
                    # stop() takes up to its timeout plus 2 x THREAD_TIMEOUT (4 s)
                    left = SHUTDOWN_BUDGET - (time.monotonic() - stopped_at) - 4.0
                    await publisher.stop(timeout=max(0.5, min(STOP_TIMEOUT, left)))
        _LOGGER.info("Bridge stopped")
    return 0
