import asyncio
import json
import logging
import re
import ssl
import threading
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from bosch_homecom_mqtt_bridge import protocol
from bosch_homecom_mqtt_bridge import publisher as publisher_module
from bosch_homecom_mqtt_bridge.config import load_config
from bosch_homecom_mqtt_bridge.protocol import Kind
from bosch_homecom_mqtt_bridge.publisher import PAHO_WINDOW, MqttPublisher, _paho_client
from bosch_homecom_mqtt_bridge.topics import TopicError, Topics

CONTRACT = Path(__file__).resolve().parent.parent / "docs" / "mqtt-contract.md"
PASSWORD = "FAKE-mqtt-password-value"
DEVICE = "101506113"
MOMENT = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
TOPICS = Topics("bosch-homecom")


def section(markdown: str, heading: str) -> str:
    """Text below ``heading`` up to the next heading of any level."""
    start = markdown.index(f"\n{heading}\n") + len(heading) + 2
    match = re.compile(r"^#+ ", re.MULTILINE).search(markdown, start)
    return markdown[start : match.start() if match else len(markdown)]


def table_rows(text: str) -> list[list[str]]:
    """Data rows of the first table in ``text``."""
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("|"))
    rows = []
    for line in lines[first:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    cells = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]
    return cells[2:]  # skip header and separator


def unquote(cell: str) -> str:
    return cell[1:-1] if cell.startswith("`") and cell.endswith("`") else cell


def contract_rows() -> dict[str, dict[str, Any]]:
    rows = {}
    for name, topic, retained, qos, payload in table_rows(section(CONTRACT.read_text("utf-8"), "## Topics")):
        rows[name] = {"topic": unquote(topic), "retain": retained == "ja", "qos": int(qos), "payload": unquote(payload)}
    return rows


def assert_payload(test: unittest.TestCase, payload: bytes, expected: str) -> None:
    if expected.startswith("{"):
        decoded = json.loads(payload.decode("utf-8"))
        test.assertEqual(decoded, json.loads(expected))
        compact = json.dumps(decoded, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        test.assertEqual(payload, compact)
    else:
        test.assertEqual(payload.decode("utf-8"), expected)


# Builders per contract row; the publisher variant drives the same row through MqttPublisher.
BUILDERS: dict[str, Callable[[], protocol.Message]] = {
    "Status": lambda: protocol.status_message(TOPICS, "ready"),
    "Status mit Text": lambda: protocol.status_message(TOPICS, "auth_required", "login required"),
    "Status (LWT)": lambda: protocol.will_message(TOPICS),
    "Fehler": lambda: protocol.error_message(TOPICS, "AUTH_REQUIRED", "refresh token rejected"),
    "Zustand": lambda: protocol.state_message(
        TOPICS, DEVICE, {"temperature": 52.5, "heating": True, "mode": "eco"}, MOMENT
    ),
    "Verfügbarkeit online": lambda: protocol.availability_message(TOPICS, DEVICE, True),
    "Verfügbarkeit offline": lambda: protocol.availability_message(TOPICS, DEVICE, False),
}
PUBLISHER_CALLS: dict[str, Callable[[MqttPublisher], None]] = {
    "Status": lambda p: p.publish_status("ready"),
    "Status mit Text": lambda p: p.publish_status("auth_required", "login required"),
    "Fehler": lambda p: p.publish_error("AUTH_REQUIRED", "refresh token rejected"),
    "Zustand": lambda p: p.publish_state(DEVICE, {"temperature": 52.5, "heating": True, "mode": "eco"}, MOMENT),
    "Verfügbarkeit online": lambda p: p.publish_availability(DEVICE, True),
    "Verfügbarkeit offline": lambda p: p.publish_availability(DEVICE, False),
}


class ContractTableTest(unittest.TestCase):
    def test_every_documented_row_has_a_builder(self) -> None:
        self.assertEqual(set(contract_rows()), set(BUILDERS))

    def test_builders_match_contract(self) -> None:
        for name, row in contract_rows().items():
            with self.subTest(row=name):
                message = BUILDERS[name]()
                self.assertEqual(message.topic, row["topic"])
                self.assertEqual(message.retain, row["retain"])
                self.assertEqual(message.qos, row["qos"])
                self.assertEqual(message.qos, 1)
                assert_payload(self, message.payload, row["payload"])

    def test_status_values_match_contract(self) -> None:
        text = section(CONTRACT.read_text("utf-8"), "#### Statuswerte")
        documented = {unquote(row[0]) for row in table_rows(text)}
        self.assertEqual(documented, set(protocol.STATUS_STATES))


class ProtocolTest(unittest.TestCase):
    def test_status_retained_and_connected_flag(self) -> None:
        for state in protocol.STATUS_STATES:
            with self.subTest(state=state):
                message = protocol.status_message(TOPICS, state)
                self.assertTrue(message.retain)
                self.assertIs(message.kind, Kind.STATUS)
                self.assertEqual(json.loads(message.payload)["connected"], state != "disconnected")
                self.assertNotIn("message", json.loads(message.payload))

    def test_unknown_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            protocol.status_message(TOPICS, "sleeping")

    def test_error_not_retained_and_code_format(self) -> None:
        self.assertFalse(protocol.error_message(TOPICS, "AUTH_REQUIRED", "x").retain)
        for code in ("auth_required", "", "AUTH-REQUIRED", "1AUTH"):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    protocol.error_message(TOPICS, code, "x")

    def test_state_timestamp_is_utc(self) -> None:
        local = datetime(2026, 10, 4, 14, 0, 30, 999, tzinfo=timezone(timedelta(hours=2)))
        payload = json.loads(protocol.state_message(TOPICS, DEVICE, {"a": 1}, local).payload)
        self.assertEqual(payload["updated_at"], "2026-10-04T12:00:30Z")

    def test_state_default_timestamp(self) -> None:
        payload = json.loads(protocol.state_message(TOPICS, DEVICE, {}).payload)
        parsed = datetime.strptime(payload["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertLess(abs(datetime.now(timezone.utc) - parsed), timedelta(minutes=1))

    def test_state_must_be_flat(self) -> None:
        bad_values = [
            {"nested": {"a": 1}},
            {"list": [1, 2]},
            {"updated_at": "2026-01-01T00:00:00Z"},
            {"": 1},
        ]
        for values in bad_values:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    protocol.state_message(TOPICS, DEVICE, values, MOMENT)

    def test_state_accepts_scalars_and_null(self) -> None:
        values = {"t": 1.5, "n": 3, "b": False, "s": "eco", "missing": None}
        payload = json.loads(protocol.state_message(TOPICS, DEVICE, values, MOMENT).payload)
        self.assertEqual(payload, {**values, "updated_at": "2026-10-04T12:00:00Z"})

    def test_non_finite_numbers_become_null(self) -> None:
        values = {"nan": float("nan"), "inf": float("inf"), "ninf": float("-inf"), "ok": 1.5}
        payload = json.loads(protocol.state_message(TOPICS, DEVICE, values, MOMENT).payload)
        self.assertEqual(payload, {"nan": None, "inf": None, "ninf": None, "ok": 1.5, "updated_at": "2026-10-04T12:00:00Z"})

    def test_naive_timestamp_rejected(self) -> None:
        with self.assertRaises(ValueError):
            protocol.state_message(TOPICS, DEVICE, {}, datetime(2026, 10, 4, 12, 0))

    def test_invalid_device_ids_rejected(self) -> None:
        for device_id in ("a/b", "a+b", "a#b", "$a", "a\x00b"):
            with self.subTest(device_id=device_id):
                with self.assertRaises(TopicError):
                    protocol.state_message(TOPICS, device_id, {}, MOMENT)
                with self.assertRaises(TopicError):
                    protocol.availability_message(TOPICS, device_id, True)


class FakeInfo:
    """Like paho's MQTTMessageInfo: ``wait_for_publish`` blocks until the broker's PUBACK."""

    def __init__(self, rc: int, mid: int, topic: str) -> None:
        self.rc = rc
        self.mid = mid
        self.topic = topic
        self.returns_early = False  # paho returns from wait_for_publish() at its timeout without raising
        self._acked = threading.Event()

    def wait_for_publish(self, timeout: float | None = None) -> None:
        if self.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
            raise ValueError("not queued")
        if self.rc > 0:
            raise RuntimeError("publish failed")
        self._acked.wait(0 if self.returns_early else timeout)

    def is_published(self) -> bool:
        return self._acked.is_set()


class FakeSocket:
    def __init__(self, client: "FakeClient") -> None:
        self.client = client

    def close(self) -> None:
        self.client.events.append("socket_close")
        if self.client.release_on_close:
            self.client.loop_exit.set()


class FakeClient:
    """Records calls like paho's Client; broker events are fired from a foreign thread.

    ``auto_ack`` acknowledges every publish at once. Otherwise ``window`` emulates
    ``max_queued_messages_set`` and ``ack_all`` sends the PUBACKs. ``block_loop_stop`` emulates paho's
    network thread, which keeps running while QoS 1 messages are unacknowledged and the socket is open.
    """

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_publish: Any = None
        self.will: tuple[str, bytes, int, bool] | None = None
        self.credentials: tuple[str, str | None] | None = None
        self.tls = False
        self.reconnect_delay: tuple[int, int] | None = None
        self.max_queued: int | None = None
        self.endpoint: tuple[str, int, int] | None = None
        self.loop_running = False
        self.disconnected = False
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.publish_threads: set[int] = set()
        self.rcs: list[int] = []
        self.attempts = 0
        self.auto_ack = True
        self.window: int | None = None
        self.unacked: list[FakeInfo] = []
        self.window_rejections = 0
        self.publish_errors: list[BaseException] = []
        self.publish_gate: threading.Event | None = None
        self.wait_returns_early = False
        self.block_loop_stop = False
        self.release_on_close = True
        self.loop_exit = threading.Event()
        self.events: list[str] = []
        self._lock = threading.Lock()
        self._mid = 0

    def will_set(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.credentials = (username, password)

    def tls_set(self) -> None:
        self.tls = True

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def max_queued_messages_set(self, size: int) -> None:
        self.max_queued = size

    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None:
        self.endpoint = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_running = True

    def loop_stop(self) -> None:
        if self.block_loop_stop:
            self.loop_exit.wait(10)
        self.loop_running = False
        self.events.append("loop_stop")

    def disconnect(self) -> None:
        self.disconnected = True
        self.events.append("disconnect")
        self.loop_exit.set()

    def socket(self) -> FakeSocket:
        return FakeSocket(self)

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> FakeInfo:
        self.publish_threads.add(threading.get_ident())
        if self.publish_gate is not None:
            self.publish_gate.wait(10)
        with self._lock:
            self.attempts += 1
            if self.publish_errors:
                raise self.publish_errors.pop(0)
            rc = self.rcs.pop(0) if self.rcs else mqtt.MQTT_ERR_SUCCESS
            if rc == mqtt.MQTT_ERR_SUCCESS and self.window is not None and len(self.unacked) >= self.window:
                rc = mqtt.MQTT_ERR_QUEUE_SIZE
                self.window_rejections += 1
            self._mid += 1
            info = FakeInfo(rc, self._mid, topic)
            info.returns_early = self.wait_returns_early
            if rc == mqtt.MQTT_ERR_SUCCESS:
                self.published.append((topic, payload, qos, retain))
                self.events.append(f"publish:{topic}")
                if self.auto_ack:
                    info._acked.set()
                    self.events.append(f"ack:{topic}")
                else:
                    self.unacked.append(info)
            return info

    def ack_all(self) -> int:
        """Send the PUBACK for every unacknowledged message, from the calling (foreign) thread."""
        with self._lock:
            infos, self.unacked = self.unacked, []
            for info in infos:
                info._acked.set()
                self.events.append(f"ack:{info.topic}")
        for info in infos:
            self.on_publish(self, None, info.mid, ReasonCode(PacketTypes.PUBACK, identifier=0), None)
        return len(infos)

    def _fire(self, callback: Any, *args: Any) -> None:
        thread = threading.Thread(target=callback, args=(self, None, *args))
        thread.start()
        thread.join()

    def fire_connect(self, reason: int = 0) -> None:
        self._fire(self.on_connect, mqtt.ConnectFlags(False), ReasonCode(PacketTypes.CONNACK, identifier=reason), None)

    def fire_disconnect(self) -> None:
        self._fire(self.on_disconnect, mqtt.DisconnectFlags(False), ReasonCode(PacketTypes.DISCONNECT, identifier=0), None)

    def fire_publish(self) -> None:
        self._fire(self.on_publish, 1, ReasonCode(PacketTypes.PUBACK, identifier=0), None)


async def eventually(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def run_pending_callbacks() -> None:
    """Let the loop run every callback already scheduled (e.g. by ``call_soon_threadsafe``)."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class PublisherTestBase(unittest.IsolatedAsyncioTestCase):
    env: dict[str, str] = {}

    async def asyncSetUp(self) -> None:
        self.clients: list[FakeClient] = []
        self.publisher = MqttPublisher(load_config(self.env), client_factory=self.make_client)
        await self.publisher.start()
        self.client = self.clients[0]

    async def asyncTearDown(self) -> None:
        await self.publisher.stop(timeout=0.5)

    def make_client(self, client_id: str) -> FakeClient:
        client = FakeClient(client_id)
        self.clients.append(client)
        return client

    async def connect(self) -> None:
        self.client.fire_connect()
        await eventually(lambda: self.publisher.connected)


class PublisherSetupTest(PublisherTestBase):
    env = {
        "MQTT_URL": "mqtts://broker.example",
        "MQTT_USERNAME": "bridge",
        "MQTT_PASSWORD": PASSWORD,
        "MQTT_CLIENT_ID": "bridge-1",
    }

    async def test_will_is_retained_disconnected_status(self) -> None:
        expected = contract_rows()["Status (LWT)"]
        assert self.client.will is not None
        topic, payload, qos, retain = self.client.will
        self.assertEqual((topic, qos, retain), (expected["topic"], expected["qos"], expected["retain"]))
        assert_payload(self, payload, expected["payload"])
        self.assertEqual(json.loads(payload), {"state": "disconnected", "connected": False})

    async def test_connection_settings(self) -> None:
        self.assertEqual(self.client.client_id, "bridge-1")
        self.assertEqual(self.client.credentials, ("bridge", PASSWORD))
        self.assertTrue(self.client.tls)
        self.assertEqual(self.client.endpoint, ("broker.example", 8883, 60))
        self.assertEqual(self.client.reconnect_delay, (1, 60))
        self.assertEqual(self.client.max_queued, PAHO_WINDOW)
        self.assertTrue(self.client.loop_running)

    async def test_stop_publishes_disconnected_and_disconnects(self) -> None:
        await self.connect()
        await self.publisher.stop(timeout=1.0)
        topic, payload, qos, retain = self.client.published[-1]
        self.assertEqual((topic, qos, retain), ("bosch-homecom/event/status", 1, True))
        self.assertEqual(json.loads(payload)["state"], "disconnected")
        self.assertTrue(self.client.disconnected)
        self.assertFalse(self.client.loop_running)
        events = self.client.events
        self.assertLess(events.index("ack:bosch-homecom/event/status"), events.index("disconnect"))
        self.assertNotIn("socket_close", events)


class PublisherPlainTest(PublisherTestBase):
    env = {"MQTT_URL": "mqtt://broker.example", "MQTT_QUEUE_SIZE": "10"}

    async def test_no_tls_and_no_credentials_for_plain_url(self) -> None:
        self.assertFalse(self.client.tls)
        self.assertIsNone(self.client.credentials)
        self.assertEqual(self.client.endpoint, ("broker.example", 1883, 60))

    async def test_publishes_follow_contract_table(self) -> None:
        await self.connect()
        rows = contract_rows()
        for name, call in PUBLISHER_CALLS.items():
            with self.subTest(row=name):
                before = len(self.client.published)
                call(self.publisher)
                await eventually(lambda: len(self.client.published) > before)
                topic, payload, qos, retain = self.client.published[-1]
                self.assertEqual((topic, qos, retain), (rows[name]["topic"], rows[name]["qos"], rows[name]["retain"]))
                assert_payload(self, payload, rows[name]["payload"])

    async def test_nothing_published_before_connect_then_in_order(self) -> None:
        self.publisher.publish_status("starting")
        self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
        await eventually(lambda: not self.publisher._wakeup.is_set())  # the drain has seen both messages
        self.assertEqual(self.client.published, [])
        await self.connect()
        await eventually(lambda: len(self.client.published) == 2)
        topics = [entry[0] for entry in self.client.published]
        # the queued status is the current one, so no replay goes in front of it
        self.assertEqual(topics, ["bosch-homecom/event/status", "bosch-homecom/101506113/state"])
        self.assertEqual(json.loads(self.client.published[0][1])["state"], "starting")

    async def test_status_republished_after_reconnect(self) -> None:
        await self.connect()
        self.publisher.publish_status("ready")
        await eventually(lambda: len(self.client.published) == 1)
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING):
            self.client.fire_disconnect()
            await eventually(lambda: not self.publisher.connected)
        await self.connect()
        await eventually(lambda: len(self.client.published) == 2)
        self.assertEqual(self.client.published[1], self.client.published[0])
        self.assertTrue(self.client.published[1][3])

    async def test_refused_connection_stays_disconnected(self) -> None:
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING):
            self.client.fire_connect(reason=135)
            await run_pending_callbacks()
        self.assertFalse(self.publisher.connected)

    async def test_full_paho_window_is_retried_after_puback(self) -> None:
        await self.connect()
        self.client.rcs = [mqtt.MQTT_ERR_QUEUE_SIZE]
        self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
        await eventually(lambda: self.client.attempts == 1 and len(self.publisher.queued()) == 1)
        self.assertEqual(self.client.published, [])
        self.client.fire_publish()
        await eventually(lambda: len(self.client.published) == 1)
        self.assertEqual(self.publisher.queued(), [])

    async def test_no_conn_counts_as_handed_over(self) -> None:
        await self.connect()
        self.client.rcs = [mqtt.MQTT_ERR_NO_CONN]
        self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
        await eventually(lambda: not self.publisher.queued())

    async def test_publish_runs_off_the_event_loop_thread(self) -> None:
        await self.connect()
        self.publisher.publish_status("ready")
        await eventually(lambda: len(self.client.published) == 1)
        self.assertNotIn(threading.get_ident(), self.client.publish_threads)

    async def test_overflow_while_disconnected_keeps_latest_status(self) -> None:
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING) as logs:
            self.publisher.publish_status("starting")
            for n in range(30):
                self.publisher.publish_state(DEVICE, {"n": n}, MOMENT)
                self.publisher.publish_error("AUTH_REQUIRED", f"e{n}")
            self.publisher.publish_status("auth_required")
        self.assertTrue(any("dropped state" in line for line in logs.output))
        queued = self.publisher.queued()
        self.assertEqual(len(queued), 10)
        self.assertEqual([m.kind for m in queued].count(Kind.STATUS), 1)  # "starting" replaced, not dropped
        self.assertNotIn(Kind.STATE, {m.kind for m in queued})
        await self.connect()
        await eventually(lambda: len(self.client.published) == len(queued))
        states = [json.loads(p)["state"] for t, p, _, _ in self.client.published if t.endswith("/event/status")]
        self.assertEqual(states, ["auth_required"])

    async def test_state_with_nan_is_published_with_null(self) -> None:
        await self.connect()
        self.publisher.publish_state(DEVICE, {"t": float("nan"), "n": 1}, MOMENT)
        await eventually(lambda: len(self.client.published) == 1)
        self.assertEqual(json.loads(self.client.published[0][1]), {"t": None, "n": 1, "updated_at": "2026-10-04T12:00:00Z"})


class PublisherStopTest(PublisherTestBase):
    env = {"MQTT_URL": "mqtt://broker.example"}

    def status_states(self) -> list[str]:
        return [json.loads(p)["state"] for t, p, _, _ in self.client.published if t.endswith("/event/status")]

    async def test_stop_waits_for_puback_with_backlog_beyond_window(self) -> None:
        client = self.client
        client.auto_ack = False
        client.window = PAHO_WINDOW
        await self.connect()
        backlog = PAHO_WINDOW * 2 + 50
        for n in range(backlog):
            self.publisher.publish_state(DEVICE, {"n": n}, MOMENT)
        await eventually(lambda: len(client.unacked) == PAHO_WINDOW and client.window_rejections > 0)
        stopper = asyncio.create_task(self.publisher.stop(timeout=5.0))
        done = threading.Event()

        def broker() -> None:
            while not done.is_set():
                if not client.ack_all():
                    done.wait(0.001)

        acker = threading.Thread(target=broker)
        acker.start()
        try:
            await asyncio.wait_for(stopper, 5.0)
        finally:
            done.set()
            acker.join()
        self.assertEqual(len(client.published), backlog + 1)
        self.assertEqual(self.status_states(), ["disconnected"])
        self.assertEqual(client.published[-1][0], "bosch-homecom/event/status")
        events = client.events
        self.assertLess(events.index("ack:bosch-homecom/event/status"), events.index("disconnect"))
        self.assertNotIn("socket_close", events)
        self.assertIn("loop_stop", events)

    async def test_stop_without_puback_closes_without_disconnect(self) -> None:
        client = self.client
        client.auto_ack = False
        client.block_loop_stop = True  # paho's thread stays alive while QoS 1 messages are unacknowledged
        await self.connect()
        started = asyncio.get_running_loop().time()
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING) as logs:
            await self.publisher.stop(timeout=0.2)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(self.status_states(), ["disconnected"])  # handed over, but never acknowledged
        self.assertNotIn("disconnect", client.events)
        self.assertFalse(client.disconnected)
        self.assertIn("socket_close", client.events)
        self.assertIn("loop_stop", client.events)
        self.assertTrue(any("no PUBACK" in line for line in logs.output))
        self.assertLess(elapsed, 0.2 + publisher_module.THREAD_TIMEOUT)

    async def test_wait_returning_without_puback_is_not_an_ack(self) -> None:
        self.client.auto_ack = False
        self.client.wait_returns_early = True
        await self.connect()
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING) as logs:
            await self.publisher.stop(timeout=2.0)
        self.assertNotIn("disconnect", self.client.events)
        self.assertIn("socket_close", self.client.events)
        self.assertTrue(any("no PUBACK" in line for line in logs.output))

    async def test_hung_network_thread_does_not_block_stop(self) -> None:
        client = self.client
        client.auto_ack = False
        client.block_loop_stop = True
        client.release_on_close = False
        self.addCleanup(client.loop_exit.set)
        await self.connect()
        with mock.patch.object(publisher_module, "THREAD_TIMEOUT", 0.1):
            with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING) as logs:
                await asyncio.wait_for(self.publisher.stop(timeout=0.1), 2.0)
        self.assertTrue(any("did not stop" in line for line in logs.output))
        self.assertNotIn("disconnect", client.events)

    async def test_disconnect_before_hand_over_ends_wait(self) -> None:
        client = self.client
        client.auto_ack = False
        client.window = 1
        await self.connect()
        self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
        await eventually(lambda: len(client.unacked) == 1)
        started = asyncio.get_running_loop().time()
        stopper = asyncio.create_task(self.publisher.stop(timeout=5.0))
        await eventually(lambda: client.window_rejections > 0)  # final status waits for the window
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING):
            client.fire_disconnect()
            await asyncio.wait_for(stopper, 2.0)
        self.assertLess(asyncio.get_running_loop().time() - started, 2.0)
        self.assertNotIn("disconnect", client.events)

    async def test_stop_while_disconnected_does_not_wait(self) -> None:
        started = asyncio.get_running_loop().time()
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING) as logs:
            await self.publisher.stop(timeout=5.0)
        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
        self.assertTrue(any("1 queued messages were not delivered" in line for line in logs.output))
        self.assertEqual(self.client.published, [])
        self.assertNotIn("disconnect", self.client.events)

    async def test_publish_exception_is_logged_and_drain_continues(self) -> None:
        await self.connect()
        self.client.publish_errors = [RuntimeError("boom")]
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.ERROR) as logs:
            self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
            self.publisher.publish_state(DEVICE, {"n": 2}, MOMENT)
            await eventually(lambda: len(self.client.published) == 1)
        self.assertEqual(json.loads(self.client.published[0][1])["n"], 2)
        self.assertTrue(any("RuntimeError" in line and "dropped" in line for line in logs.output))

    async def test_unexpected_drain_failure_is_logged_and_drain_survives(self) -> None:
        await self.connect()
        original = self.client.publish
        broken = [True]

        def publish(*args: Any, **kwargs: Any) -> Any:
            if broken.pop() if broken else False:
                return object()  # no .rc: fails outside the per-message handling
            return original(*args, **kwargs)

        self.client.publish = publish  # type: ignore[method-assign]
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.ERROR) as logs:
            self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
            await eventually(lambda: any("drain step failed" in line for line in logs.output))
        self.publisher.publish_state(DEVICE, {"n": 2}, MOMENT)
        await eventually(lambda: len(self.client.published) == 1)
        assert self.publisher._drain_task is not None
        self.assertFalse(self.publisher._drain_task.done())

    async def test_drain_task_end_is_reported(self) -> None:
        async def crash() -> None:
            raise RuntimeError("drain crashed")

        task = asyncio.create_task(crash())
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.ERROR) as logs:
            task.add_done_callback(self.publisher._drain_done)
            with self.assertRaises(RuntimeError):
                await task
            await run_pending_callbacks()
        self.assertTrue(any("drain task ended unexpectedly" in line for line in logs.output))

    async def test_cancel_during_hand_over_is_logged(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.client.publish_gate = gate  # paho blocks inside publish()
        await self.connect()
        self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
        await eventually(lambda: bool(self.client.publish_threads))
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING) as logs:
            await self.publisher.stop(timeout=0.1)
        output = "\n".join(logs.output)
        self.assertIn("interrupted the hand-over of a state message", output)
        self.assertIn("1 queued messages were not delivered", output)

    async def test_late_callbacks_and_publishes_after_stop_are_ignored(self) -> None:
        await self.connect()
        await self.publisher.stop(timeout=1.0)
        published = list(self.client.published)
        self.client.fire_connect()
        self.client.fire_publish()
        self.client.fire_disconnect()
        self.publisher.publish_status("ready")
        self.publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
        await run_pending_callbacks()
        self.assertFalse(self.publisher.connected)
        self.assertEqual(self.publisher.queued(), [])
        self.assertEqual(self.client.published, published)

    async def test_reconnect_during_stop_is_ignored(self) -> None:
        await self.connect()
        self.publisher.publish_status("ready")
        await eventually(lambda: len(self.client.published) == 1)
        self.client.auto_ack = False
        stopper = asyncio.create_task(self.publisher.stop(timeout=0.5))
        await eventually(lambda: len(self.client.published) == 2)  # final status handed over, no PUBACK yet
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING):
            self.client.fire_disconnect()
            await eventually(lambda: not self.publisher.connected)
        self.client.fire_connect()
        await run_pending_callbacks()
        self.assertFalse(self.publisher.connected)  # no reconnect and no status replay while stopping
        await stopper
        self.assertEqual(self.status_states(), ["ready", "disconnected"])
        self.assertNotIn("disconnect", self.client.events)


class PublisherSecretsTest(unittest.IsolatedAsyncioTestCase):
    async def test_password_never_logged(self) -> None:
        clients: list[FakeClient] = []

        def factory(client_id: str) -> FakeClient:
            clients.append(FakeClient(client_id))
            return clients[-1]

        config = load_config({"MQTT_USERNAME": "bridge", "MQTT_PASSWORD": PASSWORD, "LOG_LEVEL": "debug"})
        publisher = MqttPublisher(config, client_factory=factory)
        with self.assertLogs("bosch_homecom_mqtt_bridge", logging.DEBUG) as logs:
            await publisher.start()
            clients[0].fire_connect()
            await eventually(lambda: publisher.connected)
            clients[0].fire_connect(reason=135)
            clients[0].fire_disconnect()
            await eventually(lambda: not publisher.connected)
            await publisher.stop(timeout=0.1)
        self.assertTrue(logs.output)
        for line in logs.output:
            self.assertNotIn(PASSWORD, line)

    async def test_password_without_username_is_ignored_with_warning(self) -> None:
        clients: list[FakeClient] = []

        def factory(client_id: str) -> FakeClient:
            clients.append(FakeClient(client_id))
            return clients[-1]

        publisher = MqttPublisher(load_config({"MQTT_PASSWORD": PASSWORD}), client_factory=factory)
        with self.assertLogs("bosch_homecom_mqtt_bridge.publisher", logging.WARNING) as logs:
            await publisher.start()
        await publisher.stop(timeout=0.1)
        self.assertIsNone(clients[0].credentials)
        self.assertNotIn(PASSWORD, "\n".join(logs.output))


class OfflinePahoClient(mqtt.Client):
    """Real paho client whose network loop never starts, so no connection is attempted."""

    def loop_start(self) -> mqtt.MQTTErrorCode:
        return mqtt.MQTT_ERR_SUCCESS

    def loop_stop(self) -> mqtt.MQTTErrorCode:
        return mqtt.MQTT_ERR_SUCCESS


class PahoCompatibilityTest(unittest.IsolatedAsyncioTestCase):
    def test_default_factory_sets_protocol_and_session_explicitly(self) -> None:
        with mock.patch.object(publisher_module.mqtt, "Client") as client_class:
            _paho_client("bridge")
        kwargs = client_class.call_args.kwargs
        self.assertEqual(kwargs["protocol"], mqtt.MQTTv311)
        self.assertIs(kwargs["clean_session"], True)
        self.assertEqual(kwargs["client_id"], "bridge")

    def test_default_factory_uses_callback_api_v2(self) -> None:
        client = _paho_client("bridge")
        self.assertEqual(client._callback_api_version, mqtt.CallbackAPIVersion.VERSION2)
        self.assertEqual(client._protocol, mqtt.MQTTv311)
        self.assertTrue(client._clean_session)

    async def test_publisher_drives_real_paho_client_offline(self) -> None:
        clients: list[OfflinePahoClient] = []

        def factory(client_id: str) -> OfflinePahoClient:
            clients.append(OfflinePahoClient(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id))
            return clients[-1]

        config = load_config({"MQTT_URL": "mqtts://broker.invalid", "MQTT_USERNAME": "u", "MQTT_PASSWORD": PASSWORD})
        publisher = MqttPublisher(config, client_factory=factory)
        await publisher.start()
        client = clients[0]
        self.assertEqual(client.host, "broker.invalid")
        self.assertEqual(client.port, 8883)
        context = client._ssl_context
        assert context is not None
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertFalse(client._tls_insecure)
        self.assertEqual(client._will_topic, b"bosch-homecom/event/status")
        self.assertTrue(client._will_retain)
        self.assertEqual(client._will_qos, 1)
        # Fire paho's real callback signature (VERSION2) through the publisher.
        client.on_connect(client, None, mqtt.ConnectFlags(False), ReasonCode(PacketTypes.CONNACK, identifier=0), None)
        await eventually(lambda: publisher.connected)
        publisher.publish_state(DEVICE, {"n": 1}, MOMENT)
        await eventually(lambda: not publisher.queued())  # NO_CONN: kept by paho for resend
        await publisher.stop(timeout=0.1)


if __name__ == "__main__":
    unittest.main()
