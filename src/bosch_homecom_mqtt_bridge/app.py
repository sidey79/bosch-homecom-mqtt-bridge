"""The ``run`` command: wires publisher, token manager and poller, and shuts down in order.

Shutdown (ADR 0001): SIGTERM/SIGINT set a stop event. In the ``finally`` of ``run`` the token manager
is closed first (a running refresh POST and its ``auth.json`` write finish), then the ``ClientSession``,
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
from .poller import Poller
from .publisher import STOP_TIMEOUT, MqttPublisher

_LOGGER = logging.getLogger(__name__)
SHUTDOWN_BUDGET = 18.0  # seconds, below stop_grace_period (20 s)


async def run(config: Config, stop: asyncio.Event | None = None) -> int:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    publisher = MqttPublisher(config)
    poller = Poller(config, publisher)
    session = ClientSession()
    tokens = TokenManager(
        AuthStore(config.bosch_auth_path),
        session,
        brand=config.bosch_brand,
        poll_timeout=config.bosch_poll_timeout,
        on_state=poller.on_token_state,
        on_error=publisher.publish_error,
    )
    try:
        await publisher.start()
        publisher.publish_status("starting")
        _LOGGER.info("Bridge started")
        await poller.run(tokens, stop)
    finally:
        stopped_at = time.monotonic()
        try:
            await tokens.aclose()
        finally:
            try:
                await session.close()
            finally:
                # stop() takes up to its timeout plus 2 x THREAD_TIMEOUT (4 s)
                left = SHUTDOWN_BUDGET - (time.monotonic() - stopped_at) - 4.0
                await publisher.stop(timeout=max(0.5, min(STOP_TIMEOUT, left)))
        _LOGGER.info("Bridge stopped")
    return 0
