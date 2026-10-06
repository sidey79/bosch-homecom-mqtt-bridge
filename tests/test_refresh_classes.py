"""Table K: every class is triggered in the fake session through the real library refresh path.

Classification tests run ``get_token(force=True)`` of an ``auth_provider=True`` instance and feed the
exception to ``classify``. D11 option (b): retry only for K3 (429) and K4 (not sent).
"""
import asyncio
import json
import logging
import unittest
from datetime import UTC, datetime
from email.utils import format_datetime
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

from .token_fakes import (
    FakeClock,
    FakeResponse,
    FakeSession,
    ListHandler,
    connector_error,
    http_error,
    request_info,
)

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


if __name__ == "__main__":
    unittest.main()
