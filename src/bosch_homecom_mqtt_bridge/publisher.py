"""Asynchronous MQTT publisher on top of paho-mqtt 2.1 (contract: docs/mqtt-contract.md).

paho runs its network loop, including reconnect with exponential backoff, in its own thread. Every
call into paho that can wait on a lock, the CA store or a thread join runs via ``asyncio.to_thread``,
so the asyncio loop never blocks. paho callbacks hand their events back with ``call_soon_threadsafe``.

Messages first go into the bounded ``PublishQueue``. A drain task moves them to paho only while the
broker connection is up and paho has room (``max_queued_messages_set``), so the total buffer is
bounded and the overflow rule of the contract decides what is lost during an outage.

Threading: the ``publish_*`` methods, ``start`` and ``stop`` must be called from the thread running the
asyncio loop. Only the paho callbacks run in paho's thread, and they touch nothing but
``call_soon_threadsafe``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt

from . import protocol
from .config import Config
from .protocol import Message
from .publish_queue import PublishQueue
from .topics import Topics

log = logging.getLogger(__name__)

KEEPALIVE = 60
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 60
PAHO_WINDOW = 100  # unacknowledged QoS 1 messages handed to paho at most
STOP_TIMEOUT = 5.0  # seconds to wait for the PUBACK of the final status; well below stop_grace_period (20 s)
THREAD_TIMEOUT = 2.0  # seconds granted to a blocking paho call during shutdown
DEFAULT_PORTS = {"mqtt": 1883, "mqtts": 8883}
_HANDED_OVER = (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN)  # NO_CONN: paho keeps it and resends


def _paho_client(client_id: str) -> mqtt.Client:
    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
        clean_session=True,
        reconnect_on_failure=True,
    )


async def _in_daemon_thread(func: Callable[[], object], timeout: float) -> bool:
    """Run a blocking paho call in a daemon thread; return False if it is still running after ``timeout``.

    Unlike ``asyncio.to_thread``, a hung call cannot keep the process alive at exit.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def finish() -> None:
        if not done.done():
            done.set_result(None)

    def run() -> None:
        try:
            func()
        except Exception:
            log.exception("MQTT shutdown call %s failed", getattr(func, "__name__", "?"))
        finally:
            try:
                loop.call_soon_threadsafe(finish)
            except RuntimeError:
                pass  # loop already closed

    threading.Thread(target=run, name="mqtt-shutdown", daemon=True).start()
    try:
        await asyncio.wait_for(asyncio.shield(done), timeout)
    except TimeoutError:
        return False
    return True


class MqttPublisher:
    def __init__(self, config: Config, client_factory: Callable[[str], Any] = _paho_client) -> None:
        url = urlsplit(config.mqtt_url)
        self._host = url.hostname or ""
        self._port = url.port or DEFAULT_PORTS[url.scheme]
        self._tls = url.scheme == "mqtts"
        self._config = config
        self._client_factory = client_factory
        self.topics = Topics(config.mqtt_base_topic)
        self._queue = PublishQueue(config.mqtt_queue_size)
        self._status: Message | None = None
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._stopping = False
        self._wakeup = asyncio.Event()
        self._drain_task: asyncio.Task[None] | None = None
        self._final: Message | None = None
        self._final_info: asyncio.Future[Any] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def queued(self) -> list[Message]:
        return self._queue.snapshot()

    async def start(self) -> None:
        """Configure the client (LWT, credentials, TLS, backoff) and start connecting in the background."""
        self._loop = asyncio.get_running_loop()
        client = self._client_factory(self._config.mqtt_client_id)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_publish = self._on_publish
        will = protocol.will_message(self.topics)
        client.will_set(will.topic, will.payload, qos=will.qos, retain=will.retain)
        if self._config.mqtt_username:
            client.username_pw_set(self._config.mqtt_username, self._config.mqtt_password)
        elif self._config.mqtt_password:
            log.warning("MQTT_PASSWORD is ignored because MQTT_USERNAME is not set")
        if self._tls:
            await asyncio.to_thread(client.tls_set)  # system CA store, hostname verification on
        client.reconnect_delay_set(min_delay=RECONNECT_MIN_DELAY, max_delay=RECONNECT_MAX_DELAY)
        client.max_queued_messages_set(PAHO_WINDOW)
        client.connect_async(self._host, self._port, keepalive=KEEPALIVE)
        self._client = client
        await asyncio.to_thread(client.loop_start)
        self._drain_task = asyncio.create_task(self._drain(), name="mqtt-drain")
        self._drain_task.add_done_callback(self._drain_done)
        log.info("connecting to MQTT broker %s:%d (tls=%s)", self._host, self._port, self._tls)

    async def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Publish status ``disconnected``, wait for its PUBACK, then disconnect cleanly.

        Without a PUBACK within ``timeout`` (e.g. broker gone or backlog too long) the connection is closed
        without DISCONNECT, so the broker publishes the last will instead. ``timeout`` plus twice
        ``THREAD_TIMEOUT`` must stay below the container's stop grace period.
        """
        if self._client is None or self._stopping:
            return
        loop = asyncio.get_running_loop()
        self._final = protocol.status_message(self.topics, "disconnected")
        self._final_info = loop.create_future()
        self._enqueue(self._final)
        self._stopping = True  # from here on: publish_* and connect events are ignored
        acked = False
        if self._connected:
            try:
                acked = await asyncio.wait_for(self._final_acked(loop.time() + timeout), timeout)
            except TimeoutError:
                pass
            except Exception:
                log.exception("waiting for the MQTT PUBACK failed")
        await self._stop_drain()
        if self._queue:
            log.warning("MQTT stop: %d queued messages were not delivered", len(self._queue))
        client, self._client = self._client, None
        try:
            if acked:
                await _in_daemon_thread(client.disconnect, THREAD_TIMEOUT)
            elif self._connected:
                log.warning(
                    "no PUBACK for the disconnected status within %.1fs; closing without DISCONNECT "
                    "so that the broker publishes the last will",
                    timeout,
                )
            # Start loop_stop() first: it sets paho's terminate flag, so the closed socket below does not
            # lead to a reconnect.
            stopper = asyncio.ensure_future(_in_daemon_thread(client.loop_stop, THREAD_TIMEOUT))
            await asyncio.sleep(0)
            if not acked:
                # loop_stop() keeps the paho thread alive while QoS 1 messages are unacknowledged;
                # closing the socket ends it without sending DISCONNECT.
                sock = client.socket()
                if sock is not None:
                    sock.close()
            if not await stopper:
                log.warning("MQTT network thread did not stop within %.1fs", THREAD_TIMEOUT)
        except Exception:
            log.exception("MQTT shutdown failed")
        self._connected = False
        self._loop = None  # late paho callbacks are dropped from here on

    async def _final_acked(self, deadline: float) -> bool:
        """Wait until the drain hands over the final status, then for its PUBACK."""
        assert self._final_info is not None
        info = await self._final_info
        if info is None:
            return False  # connection lost before the hand-over
        remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
        try:
            await asyncio.to_thread(info.wait_for_publish, remaining)
            return bool(info.is_published())
        except (RuntimeError, ValueError):
            return False  # paho reports the message as not sendable

    async def _stop_drain(self) -> None:
        if self._drain_task is None:
            return
        self._drain_task.cancel()
        try:
            await self._drain_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # already logged by _drain_done

    def publish_status(self, state: str, message: str | None = None) -> None:
        status = protocol.status_message(self.topics, state, message)
        if self._enqueue(status):
            self._status = status

    def publish_error(self, code: str, message: str) -> None:
        self._enqueue(protocol.error_message(self.topics, code, message))

    def publish_state(self, device_id: str, values: Mapping[str, object], updated_at: datetime | None = None) -> None:
        self._enqueue(protocol.state_message(self.topics, device_id, values, updated_at))

    def publish_availability(self, device_id: str, online: bool) -> None:
        self._enqueue(protocol.availability_message(self.topics, device_id, online))

    def _enqueue(self, message: Message) -> bool:
        if self._stopping:
            log.debug("MQTT publisher is stopping; %s message for %s ignored", message.kind.value, message.topic)
            return False
        dropped = self._queue.put(message)
        if dropped is not None:
            log.warning("MQTT queue full; dropped %s message for %s", dropped.kind.value, dropped.topic)
        self._wakeup.set()
        return True

    async def _drain(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            try:
                await self._hand_over()
            except Exception:
                log.exception("MQTT drain step failed; retrying on the next wakeup")

    async def _hand_over(self) -> None:
        while self._connected and self._queue:
            message = self._queue.pop()
            try:
                info = await asyncio.to_thread(
                    self._client.publish, message.topic, message.payload, qos=message.qos, retain=message.retain
                )
            except asyncio.CancelledError:
                log.warning(
                    "MQTT shutdown interrupted the hand-over of a %s message for %s; it may be lost",
                    message.kind.value,
                    message.topic,
                )
                raise
            except Exception as error:
                log.error("MQTT publish failed for %s (%s); message dropped", message.topic, type(error).__name__)
                continue
            if info.rc not in _HANDED_OVER:
                self._queue.push_front(message)  # paho window full or transient error; retry on wakeup
                return
            if message is self._final and self._final_info is not None and not self._final_info.done():
                self._final_info.set_result(info)

    def _drain_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        log.error("MQTT drain task ended unexpectedly", exc_info=error)

    def _set_connected(self, connected: bool) -> None:
        if connected and self._stopping:
            return  # no replay and no new hand-over once stop() has begun, also for late callbacks
        if connected == self._connected:
            return
        self._connected = connected
        if connected:
            log.info("connected to MQTT broker %s:%d", self._host, self._port)
            if self._status is not None:
                self._queue.push_front(self._status)  # restore the status the LWT may have overwritten
        else:
            log.warning("disconnected from MQTT broker; reconnecting with backoff")
            if self._final_info is not None and not self._final_info.done():
                self._final_info.set_result(None)  # stop() need not wait for a PUBACK any more
        self._wakeup.set()

    def _call_in_loop(self, callback: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is None:
            return  # not started yet or already stopped
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass  # loop already closed during shutdown

    # paho callbacks (CallbackAPIVersion.VERSION2), called from the paho network thread
    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if reason_code.is_failure:
            log.warning("MQTT broker refused the connection: %s", reason_code)
            return
        self._call_in_loop(self._set_connected, True)

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self._call_in_loop(self._set_connected, False)

    def _on_publish(self, client: Any, userdata: Any, mid: int, reason_code: Any, properties: Any) -> None:
        self._call_in_loop(self._wakeup.set)
