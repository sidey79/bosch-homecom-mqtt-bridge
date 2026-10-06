"""Table K: every class is triggered in the fake session through the real library refresh path.

Classification tests run ``get_token(force=True)`` of an ``auth_provider=True`` instance and feed the
exception to ``classify``. Consequence tests run the token manager and check D11 option (b):
retry only for K3 (429) and K4 (not sent); every other class leads to ``auth_required``.
"""
import asyncio
import fcntl
import json
import logging
import os
import tempfile
import unittest
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from aiohttp import (
    BaseConnector,
    ClientOSError,
    ClientPayloadError,
    ClientSession,
    ClientTimeout,
    ConnectionTimeoutError,
    ContentTypeError,
    ServerDisconnectedError,
    ServerTimeoutError,
    SocketTimeoutError,
)
from homecom_alt import ConnectionOptions, HomeComAlt, NotRespondingError
from homecom_alt import base as homecom_base

from bosch_homecom_mqtt_bridge.auth.refresh_errors import Action, RefreshClass, classify, retry_after
from bosch_homecom_mqtt_bridge.auth.token_manager import AUTH_REQUIRED, DISCONNECTED, READY

from .token_fakes import (
    FakeClock,
    FakeResponse,
    FakeSession,
    ListHandler,
    ManagerFixture,
    connector_error,
    http_error,
    read_auth_file,
    request_info,
)

MARKS = {"refresh_blocked", "blocked_generation", "not_before", "refresh_posts"}

_CAPTURE = ListHandler()


def setUpModule() -> None:
    logging.getLogger().addHandler(_CAPTURE)  # keeps expected warnings out of the test output


def tearDownModule() -> None:
    logging.getLogger().removeHandler(_CAPTURE)


def raising(error: BaseException):
    def handler(session, kwargs):
        raise error

    return handler


def body(payload: object = None, error: BaseException | None = None):
    def handler(session, kwargs):
        return FakeResponse(200, payload, error)

    return handler


# Triggers per class; all go through the library's request and refresh code.
CASES: dict[str, tuple[RefreshClass, object]] = {
    "K1 400": (RefreshClass.K1, raising(http_error(400))),
    "K1 empty JSON (same as 400)": (RefreshClass.K1, body({})),
    "K2 401 from the token endpoint": (RefreshClass.K2, raising(http_error(401))),
    "K3 429": (RefreshClass.K3, raising(http_error(429, {"Retry-After": "120"}))),
    "K4 connection failed": (RefreshClass.K4, raising(connector_error())),
    "K4 connection setup timed out": (RefreshClass.K4, raising(ConnectionTimeoutError())),
    "K5 timeout": (RefreshClass.K5, raising(TimeoutError())),
    "K5 ServerTimeoutError": (RefreshClass.K5, raising(ServerTimeoutError())),
    "K5 socket read timeout": (RefreshClass.K5, raising(SocketTimeoutError())),
    "K5 raw timeout reading the body": (RefreshClass.K5, body(error=TimeoutError())),
    "K6 server disconnected": (RefreshClass.K6, raising(ServerDisconnectedError())),
    "K6 ClientOSError": (RefreshClass.K6, raising(ClientOSError(104, "reset"))),
    "K6 payload error": (RefreshClass.K6, body(error=ClientPayloadError("truncated"))),
    "K7 500": (RefreshClass.K7, raising(http_error(500))),
    "K8 403": (RefreshClass.K8, raising(http_error(403))),
    "K8 404": (RefreshClass.K8, raising(http_error(404))),
    "K8 502": (RefreshClass.K8, raising(http_error(502))),
    "K8 504": (RefreshClass.K8, raising(http_error(504))),
    "K9 invalid JSON": (RefreshClass.K9, body(error=json.JSONDecodeError("Expecting value", "x", 0))),
    "K9 content type": (RefreshClass.K9, body(error=ContentTypeError(request_info(), ()))),
    "K9 missing field": (RefreshClass.K9, body({"access_token": "FAKE-access-only"})),
    "K10 anything else": (RefreshClass.K10, raising(RuntimeError("FAKE-detail"))),
}


class ClassificationTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_trigger_maps_to_its_class(self) -> None:
        for name, (expected, handler) in CASES.items():
            with self.subTest(name):
                session = FakeSession(FakeClock(), token_handler=handler)
                options = ConnectionOptions(refresh_token="FAKE-refresh-sent", auth_provider=False)
                api = HomeComAlt(session, options, auth_provider=True)  # type: ignore[arg-type]
                with self.assertRaises(Exception) as ctx:
                    await api.get_token(force=True)
                self.assertEqual(classify(ctx.exception), expected)
                self.assertEqual(len(session.token_posts), 1)

    def test_only_k3_and_k4_are_retried(self) -> None:
        retried = {cls for cls in RefreshClass if cls.action is not Action.AUTH_REQUIRED}
        self.assertEqual(retried, {RefreshClass.K3, RefreshClass.K4})
        self.assertIs(RefreshClass.K3.action, Action.RETRY_AFTER)
        self.assertIs(RefreshClass.K4.action, Action.RETRY_BACKOFF)
        for cls in (RefreshClass.K0, RefreshClass.K6, RefreshClass.K10):
            self.assertIn("unclear or processed", cls.rotation)

    def test_retry_after(self) -> None:
        now = 1_600_000_000.0

        def error(headers):
            cause = http_error(429, headers)
            try:
                raise NotRespondingError("rate limited") from cause
            except NotRespondingError as wrapped:
                return wrapped

        date = format_datetime(datetime.fromtimestamp(now + 300, UTC), usegmt=True)
        cases = {"120": 120, "5": 60, "-3": 60, "nan": 60, "inf": 3600, "soon": 60, date: 300}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(retry_after(error({"Retry-After": raw}), now), expected)
        self.assertEqual(retry_after(error({}), now), 60)

    def test_raw_timeouts(self) -> None:
        self.assertIs(classify(ConnectionTimeoutError()), RefreshClass.K4)
        self.assertIs(classify(SocketTimeoutError()), RefreshClass.K5)
        self.assertIs(classify(TimeoutError()), RefreshClass.K5)


class HangingConnector(BaseConnector):
    """Connection setup that never completes; nothing leaves the process."""

    async def _create_connection(self, req, traces, timeout):  # type: ignore[override]
        await asyncio.get_running_loop().create_future()


class RealTimeoutWrappingTest(unittest.IsolatedAsyncioTestCase):
    """How aiohttp and homecom_alt 1.8.2 wrap a hanging connection setup (real ClientSession)."""

    async def refresh_error(self, timeout: ClientTimeout) -> Exception:
        async with ClientSession(connector=HangingConnector()) as session:
            options = ConnectionOptions(refresh_token="FAKE-refresh-sent", auth_provider=False)
            api = HomeComAlt(session, options, auth_provider=True)
            with mock.patch.object(homecom_base, "DEFAULT_TIMEOUT", timeout), self.assertRaises(Exception) as ctx:
                await api.get_token(force=True)
        return ctx.exception

    def test_library_sets_only_a_total_timeout(self) -> None:
        self.assertEqual(homecom_base.DEFAULT_TIMEOUT, ClientTimeout(total=15))

    async def test_connect_timeout_is_k4(self) -> None:
        error = await self.refresh_error(ClientTimeout(total=5, connect=0.05))
        self.assertIsInstance(error, NotRespondingError)
        self.assertIsInstance(error.__cause__, ConnectionTimeoutError)
        self.assertIs(classify(error), RefreshClass.K4)

    async def test_total_timeout_during_connection_setup_stays_k5(self) -> None:
        # The library's setting (total only): the total timer cannot tell setup from sending.
        error = await self.refresh_error(ClientTimeout(total=0.05))
        self.assertIsInstance(error, NotRespondingError)
        self.assertIs(type(error.__cause__), TimeoutError)
        self.assertIs(classify(error), RefreshClass.K5)


class ConsequenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ManagerFixture(Path(self._tmp.name))
        self.clock = self.fx.clock

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def park_in(self, manager, state: str) -> asyncio.Task:
        """Run ensure_fresh until the clock parks; the caller then closes the manager."""
        caller = asyncio.create_task(manager.ensure_fresh())
        blocked = asyncio.create_task(self.clock.blocked.wait())
        await asyncio.wait({caller, blocked}, return_when=asyncio.FIRST_COMPLETED)
        blocked.cancel()
        self.assertEqual(manager.state, state)
        return caller

    async def close(self, manager, caller: asyncio.Task) -> None:
        await manager.aclose()
        await asyncio.wait({caller})
        if not caller.cancelled():
            caller.exception()

    async def test_classes_without_retry_lead_to_auth_required_after_one_post(self) -> None:
        def unusable_token(session, kwargs):  # K9: 200, but the token is not a string
            return FakeResponse(200, {"access_token": 12345, "refresh_token": "FAKE-refresh-new"})

        cases = {name: case for name, case in CASES.items() if case[0].action is Action.AUTH_REQUIRED}
        cases["K9 access token not a string"] = (RefreshClass.K9, unusable_token)
        for name, (expected, handler) in cases.items():
            with self.subTest(name):
                self.fx = ManagerFixture(Path(tempfile.mkdtemp(dir=self._tmp.name)))
                self.clock = self.fx.clock
                old_access = self.fx.seed(generation=2, refresh="FAKE-refresh-seed", remaining=-1)
                before = read_auth_file(self.fx.path)
                session = self.fx.session(token_handler=handler)
                manager = self.fx.manager(session)
                self.clock.stop_at = self.clock.now + 35  # three 10 s checks, then park
                caller = await self.park_in(manager, AUTH_REQUIRED)
                self.assertEqual(manager.last_failure, expected)
                self.assertEqual(self.fx.events, ["AUTH_REQUIRED"])
                self.assertEqual(len(session.token_posts), 1)
                self.assertEqual(len(session.calls), 1)  # no cloud request while auth_required
                self.assertEqual(self.clock.sleeps, [10, 10, 10, 10])
                # Restart safety: only the block for this generation is added; tokens stay as they were.
                saved = read_auth_file(self.fx.path)
                self.assertEqual({k: v for k, v in saved.items() if k not in MARKS}, {k: v for k, v in before.items() if k not in MARKS})
                self.assertEqual((saved["refresh_blocked"], saved["blocked_generation"]), (expected.name, 2))
                self.assertEqual(len(saved["refresh_posts"]), 1)
                # In-memory tokens are those of the file again (a partial update is rolled back).
                self.assertEqual((manager._options.token, manager._options.refresh_token), (old_access, "FAKE-refresh-seed"))
                await self.close(manager, caller)

    async def test_k0_get_token_returning_without_true(self) -> None:
        self.fx.seed(refresh="FAKE-refresh-seed", remaining=-1)
        session = self.fx.session()
        manager = self.fx.manager(session)
        self.clock.stop_at = self.clock.now + 5
        with mock.patch.object(manager._refresh_api, "get_token", return_value=None):
            caller = await self.park_in(manager, AUTH_REQUIRED)
        self.assertEqual(manager.last_failure, RefreshClass.K0)
        self.assertEqual(self.fx.events, ["AUTH_REQUIRED"])
        await self.close(manager, caller)

    async def test_k3_waits_for_retry_after_then_succeeds(self) -> None:
        self.fx.seed(remaining=-1)
        answers = [http_error(429, {"Retry-After": "120"}), http_error(429, {"Retry-After": "5"})]

        def handler(session, kwargs):
            if answers:
                raise answers.pop(0)
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(session.gaps(), [120, 60])
        self.assertLessEqual(max(self.clock.sleeps), 10)  # long waits run in 10-s steps with file checks
        self.assertEqual(len(session.token_posts), 3)
        self.assertEqual(self.fx.states, [DISCONNECTED, READY])
        self.assertEqual(self.fx.events, [])

    async def test_k3_allows_at_most_three_posts_per_hour(self) -> None:
        self.fx.seed(remaining=-1)
        start = self.clock.now
        posted_at: list[float] = []

        def handler(session, kwargs):
            posted_at.append(session.clock.now - start)
            if len(posted_at) <= 3:
                raise http_error(429, {"Retry-After": "0"})
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(posted_at, [0, 60, 120, 3600])
        self.assertEqual(session.gaps(), [60, 60, 3480])
        self.assertEqual(manager.state, READY)

    async def test_k4_backs_off_from_30_s_to_15_min_without_limit(self) -> None:
        self.fx.seed(remaining=-1)
        failures = [9]

        def handler(session, kwargs):
            if failures[0]:
                failures[0] -= 1
                raise connector_error()
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(session.gaps(), [30, 60, 120, 240, 480, 900, 900, 900, 900])
        self.assertEqual(len(session.token_posts), 10)
        self.assertEqual(self.fx.states, [DISCONNECTED, READY])
        self.assertEqual(self.fx.events, [])

    async def test_connection_setup_timeout_backs_off_like_k4(self) -> None:
        self.fx.seed(remaining=-1)
        failures = [ConnectionTimeoutError()]

        def handler(session, kwargs):
            if failures:
                raise failures.pop()
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(session.gaps(), [30])
        self.assertEqual(self.fx.states, [DISCONNECTED, READY])
        self.assertEqual(self.fx.events, [])

    async def test_k4_attempts_do_not_count_towards_the_k3_limit(self) -> None:
        self.fx.seed(remaining=-1)
        answers = [connector_error(), connector_error(), connector_error(), http_error(429, {"Retry-After": "0"})]

        def handler(session, kwargs):
            if answers:
                raise answers.pop(0)
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(session.gaps(), [30, 60, 120, 60])  # not the 1 h window: one POST was sent
        self.assertEqual(manager.state, READY)

    async def test_lock_timeout_is_handled_like_k4(self) -> None:
        self.fx = ManagerFixture(Path(self._tmp.name), lock_timeout=0.2)
        self.clock = self.fx.clock
        self.fx.seed(remaining=-1)
        start = self.clock.now
        fd = os.open(self.fx.store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        session = self.fx.session()
        manager = self.fx.manager(session)

        def release(clock):
            os.close(fd)
            clock.hooks.clear()

        self.clock.hooks.append(release)
        try:
            await manager.ensure_fresh()
        finally:
            if self.clock.hooks:
                os.close(fd)
        self.assertEqual(session.post_times, [start + 30])
        self.assertEqual(self.fx.states, [DISCONNECTED, READY])

    async def test_base_exceptions_are_not_classified_and_release_the_lock(self) -> None:
        class Stop(BaseException):
            pass

        self.fx.seed(remaining=-1)
        session = self.fx.session(token_handler=raising(Stop()))
        manager = self.fx.manager(session)
        with self.assertRaises(Stop):
            await manager.ensure_fresh()
        self.assertIsNone(manager.last_failure)
        self.assertEqual(self.fx.events, [])
        self.assertIsNotNone(await self.fx.store.read_locked())  # lock is free again


if __name__ == "__main__":
    unittest.main()
