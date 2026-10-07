"""Fake cloud + fake MQTT client end to end: poller, token manager, real publisher and protocol."""
import asyncio
import json
import logging
import re
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from tenacity import Future, RetryError
from yarl import URL

from bosch_homecom_mqtt_bridge.auth.token_manager import TokenManager
from bosch_homecom_mqtt_bridge.config import load_config
from bosch_homecom_mqtt_bridge.poller import Poller, error_cause, error_name
from bosch_homecom_mqtt_bridge.publisher import MqttPublisher

from .test_protocol import FakeClient, eventually
from .token_fakes import FakeSession, ListHandler, ManagerFixture, connector_error, http_error

BASE = "bosch-homecom"
DEVICE = "101506113"
LEAK = "FAKE-leak-bearer-0123456789abcdef"
UPDATED_AT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class CloudSession(FakeSession):
    """Fake cloud: two gateways (one unsupported type), realistic wddw2 resources, optional failure."""

    gateways = [
        {"deviceId": DEVICE, "deviceType": "wddw2"},
        {"deviceId": "202", "deviceType": "rac"},
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_data: BaseException | None = None
        self.gateway_requests = 0

    async def request(self, method, url, **kwargs):
        if self.fail_data is not None and "/gateways/" in url:
            self.calls.append((method, url, kwargs))
            raise self.fail_data
        if url.endswith("/gateways/"):
            self.gateway_requests += 1
        return await super().request(method, url, **kwargs)

    def data(self, url):
        if url.endswith("/gateways/"):
            return self.gateways
        if url.endswith("/inletTemperature"):
            return {"value": 12.5, "unitOfMeasure": "C"}
        if url.endswith("/outletTemperature"):
            return {"value": 52.5, "unitOfMeasure": "C"}
        return super().data(url)


class PollerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ManagerFixture(Path(self._tmp.name))
        self.clock = self.fx.clock
        self.log = ListHandler()
        logger = logging.getLogger("bosch_homecom_mqtt_bridge")
        self._level = logger.level
        logger.addHandler(self.log)
        logger.setLevel(logging.DEBUG)
        self.clients: list[FakeClient] = []
        self.publisher = MqttPublisher(load_config({"MQTT_BASE_TOPIC": BASE}), client_factory=self._client)
        await self.publisher.start()
        self.clients[0].fire_connect()
        await eventually(lambda: self.publisher.connected)
        self.poller = Poller(load_config({}), self.publisher, interval=0.01)
        self.stop = asyncio.Event()
        self.session = CloudSession(self.clock, lock_path=self.fx.store.lock_path)
        self.tokens = TokenManager(
            self.fx.store,
            self.session,  # type: ignore[arg-type]
            brand="bosch",
            poll_timeout=300,
            clock=self.clock.time,
            sleep=self.clock.sleep,
            on_state=self.poller.on_token_state,
            on_error=self.publisher.publish_error,
            mounts=str(self.fx.mounts),
        )
        self.task: asyncio.Task | None = None

    def _client(self, client_id: str) -> FakeClient:
        client = FakeClient(client_id)
        self.clients.append(client)
        return client

    async def asyncTearDown(self) -> None:
        self.stop.set()
        if self.task is not None:
            await self.task
        await self.tokens.aclose()
        await self.publisher.stop(timeout=0.5)
        logger = logging.getLogger("bosch_homecom_mqtt_bridge")
        logger.removeHandler(self.log)
        logger.setLevel(self._level)
        self._tmp.cleanup()

    def start(self) -> None:
        self.task = asyncio.create_task(self.poller.run(self.tokens, self.stop))

    def messages(self, topic: str) -> list[tuple[bytes, bool]]:
        return [(payload, retain) for t, payload, _qos, retain in self.clients[0].published if t == topic]

    def errors(self) -> list[dict]:
        return [json.loads(p) for p, retain in self.messages(f"{BASE}/event/error") if not retain]

    async def delivered(self, condition) -> None:
        """The publisher drains asynchronously: wait until the messages reached the fake client."""
        await eventually(condition, timeout=5)

    def last(self, topic: str) -> dict:
        return json.loads(self.messages(topic)[-1][0])


class EndToEndTest(PollerTestCase):
    async def test_state_follows_the_contract_and_discovery_costs_no_token_call(self) -> None:
        self.fx.seed(remaining=3600)
        self.start()
        await eventually(lambda: len(self.messages(f"{BASE}/{DEVICE}/state")) >= 2)

        payload, retain = self.messages(f"{BASE}/{DEVICE}/state")[-1]
        state = json.loads(payload)
        self.assertTrue(retain)
        self.assertRegex(state.pop("updated_at"), UPDATED_AT)
        self.assertEqual(state["dhw1_inlet_temperature"], 12.5)
        self.assertEqual(state["dhw1_outlet_temperature"], 52.5)
        self.assertEqual(state["dhw1_operation_mode"], "eco")
        self.assertTrue(all(isinstance(v, (int, float, str, bool, type(None))) for v in state.values()))
        self.assertEqual(self.messages(f"{BASE}/{DEVICE}/availability"), [(b"online", True)])
        self.assertEqual(self.last(f"{BASE}/event/status"), {"state": "ready", "connected": True})
        # Discovery and every poll went through the fetch instance: no token call at all.
        self.assertEqual(self.session.token_posts, [])
        self.assertEqual(self.session.gateway_requests, 1)  # discovered once, not per poll
        self.assertGreater(self.session.data_requests, 10)

    async def test_unknown_device_type_is_logged_once_and_ignored(self) -> None:
        self.fx.seed(remaining=3600)
        self.start()
        await eventually(lambda: len(self.messages(f"{BASE}/{DEVICE}/state")) >= 3)
        unknown = [m for m in self.log.messages() if "no adapter" in m]
        self.assertEqual(len(unknown), 1)
        self.assertIn("rac", unknown[0])
        self.assertEqual(self.messages(f"{BASE}/202/state"), [])
        self.assertEqual(self.messages(f"{BASE}/202/availability"), [])

    async def test_configured_device_id_restricts_discovery(self) -> None:
        self.poller = Poller(load_config({"BOSCH_DEVICE_ID": "999"}), self.publisher, interval=0.01)
        self.tokens._on_state = self.poller.on_token_state
        self.fx.seed(remaining=3600)
        self.start()
        await eventually(lambda: self.session.gateway_requests == 1 and self.poller.status == "ready")
        self.assertEqual(self.messages(f"{BASE}/{DEVICE}/state"), [])


class FailureTest(PollerTestCase):
    async def ready(self) -> None:
        self.fx.seed(remaining=3600)
        self.start()
        await eventually(lambda: self.poller.status == "ready" and self.messages(f"{BASE}/{DEVICE}/state"))

    async def test_auth_required_event_status_and_offline(self) -> None:
        await self.ready()

        def reject(session, kwargs):
            raise http_error(400)

        self.session.token_handler = reject
        self.clock.stop_at = self.clock.now + 4000 + 35  # parks the wait for a login
        self.clock.now += 4000  # the access token is due: the next poll refreshes and is rejected
        await asyncio.wait_for(self.clock.blocked.wait(), 5)

        self.assertEqual(self.poller.status, "auth_required")
        await self.delivered(lambda: self.errors() and self.messages(f"{BASE}/{DEVICE}/availability")[-1][0] == b"offline")
        status = self.last(f"{BASE}/event/status")
        self.assertEqual((status["state"], status["connected"]), ("auth_required", True))
        self.assertEqual([e["code"] for e in self.errors()], ["AUTH_REQUIRED"])
        self.assertEqual(len(self.session.token_posts), 1)

    async def test_connection_timeouts_k4_report_error_status_and_offline(self) -> None:
        await self.ready()

        def unreachable(session, kwargs):
            raise connector_error()

        self.session.token_handler = unreachable
        self.clock.stop_at = self.clock.now + 4000 + 35
        self.clock.now += 4000
        await asyncio.wait_for(self.clock.blocked.wait(), 5)

        await self.delivered(lambda: self.messages(f"{BASE}/{DEVICE}/availability")[-1][0] == b"offline")
        status = self.last(f"{BASE}/event/status")
        self.assertEqual((status["state"], status["message"]), ("error", "cloud unreachable"))

    async def test_poll_failure_never_publishes_or_logs_secrets_and_recovers(self) -> None:
        await self.ready()
        info = RequestInfo(
            URL("https://cloud.invalid/x"), "GET", CIMultiDictProxy(CIMultiDict({"Authorization": f"Bearer {LEAK}"})),
            URL("https://cloud.invalid/x"),
        )
        self.session.fail_data = ClientResponseError(info, (), status=500, message=f"Bearer {LEAK}")
        await eventually(lambda: self.poller.status == "error")
        await self.delivered(lambda: self.errors() and self.messages(f"{BASE}/{DEVICE}/availability")[-1][0] == b"offline")
        await asyncio.sleep(0.1)  # several failing polls: still one event
        errors = self.errors()
        self.assertEqual([e["code"] for e in errors], ["POLL_FAILED"])  # once per transition, not per poll
        published = b"".join(payload for _t, payload, _q, _r in self.clients[0].published)
        logged = "\n".join(self.log.messages())
        for text in (published.decode(), logged):
            self.assertNotIn(LEAK, text)
            self.assertNotIn("Authorization", text)
            self.assertNotIn("Bearer", text)
        self.assertEqual(errors[0]["message"], "cloud request failed (ApiError)")

        self.session.fail_data = None
        await eventually(lambda: self.poller.status == "ready")
        await self.delivered(lambda: self.messages(f"{BASE}/{DEVICE}/availability")[-1][0] == b"online")

    async def test_stop_interrupts_a_poll_that_waits_for_a_login(self) -> None:
        # No auth file: the token manager waits for a login; the stop event must still end the poller.
        self.clock.stop_at = self.clock.now + 35
        self.start()
        await asyncio.wait_for(self.clock.blocked.wait(), 5)
        self.assertEqual(self.poller.status, "auth_required")
        self.stop.set()
        await asyncio.wait_for(self.task, 5)  # type: ignore[arg-type]
        self.assertEqual(self.session.calls, [])


class ErrorNameTest(unittest.TestCase):
    def test_type_only_and_retry_error_is_unwrapped(self) -> None:
        self.assertEqual(error_name(ConnectionResetError("Bearer x")), "ConnectionResetError")
        attempt = Future(5)
        attempt.set_exception(TimeoutError("Bearer x"))
        self.assertEqual(error_name(RetryError(attempt)), "TimeoutError")


class ErrorCauseTest(unittest.TestCase):
    @staticmethod
    def wrapped(cause: BaseException) -> Exception:
        try:
            raise RuntimeError("Bearer FAKE-secret") from cause
        except RuntimeError as error:
            return error

    def test_http_status_and_type_name_only(self) -> None:
        class Response(Exception):
            status = 429

        self.assertEqual(error_cause(self.wrapped(Response("Authorization: Bearer FAKE-secret"))), "HTTP 429")
        self.assertEqual(error_cause(self.wrapped(TimeoutError("Bearer FAKE-secret"))), "TimeoutError")
        self.assertIsNone(error_cause(RuntimeError("no cause")))

    def test_retry_error_is_unwrapped_to_the_cause_of_the_last_attempt(self) -> None:
        attempt = Future(5)
        attempt.set_exception(self.wrapped(TimeoutError("Bearer FAKE-secret")))
        self.assertEqual(error_cause(RetryError(attempt)), "TimeoutError")

    def test_a_non_numeric_status_is_not_reported(self) -> None:
        class Odd(Exception):
            status = "Bearer FAKE-secret"

        self.assertEqual(error_cause(self.wrapped(Odd())), "Odd")


if __name__ == "__main__":
    unittest.main()
