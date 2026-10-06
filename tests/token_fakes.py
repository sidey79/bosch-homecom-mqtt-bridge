"""Fake clock, fake session and helpers shared by the token manager tests.

No test talks to the real cloud: ``FakeSession`` stands in for ``aiohttp.ClientSession`` and keeps
the real URLs (the library's 400 -> None path depends on the exact token URL).
"""
from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import jwt
from aiohttp import ClientConnectorError, ClientResponseError, RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from bosch_homecom_mqtt_bridge.auth.store import AuthStore
from bosch_homecom_mqtt_bridge.auth.token_manager import TokenManager

TOKEN_URL = "https://singlekey-id.com/auth/connect/token"
JWT_KEY = "test-signing-key-for-fake-jwts-only-0123456789"
# In the past on purpose: the library's own check_jwt() uses the real clock and sees every fake token as
# expired, so a fetch instance that refreshed internally would show up in the request counts.
START = 1_600_000_000.0
# ASSUMED, NOT MEASURED: the access-token lifetime of the real login was not recorded in PR 3.
# 3600 s is an assumption used as the realistic test parameter until PR 6 measures it.
ASSUMED_LIFETIME = 3600
DEVICE = "101506113"


def make_jwt(issued_at: float, lifetime: float) -> str:
    """JWT generated at runtime (never a checked-in literal), signed with a test key."""
    return jwt.encode({"iat": int(issued_at), "exp": int(issued_at + lifetime)}, JWT_KEY, algorithm="HS256")


def jwt_exp(token: str) -> int:
    return jwt.decode(token, options={"verify_signature": False})["exp"]


class FakeClock:
    """Wall clock and sleep for the token manager.

    ``sleep`` advances the time immediately. With ``stop_at`` set, a sleep reaching that time parks
    (like a process that is still waiting) until it is cancelled; ``blocked`` is set then.
    ``hooks`` run after every advance, e.g. to simulate a login written by another process.
    """

    def __init__(self, start: float = START) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.stop_at: float | None = None
        self.blocked = asyncio.Event()
        self.hooks: list[Callable[[FakeClock], None]] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if len(self.sleeps) > 10_000:
            raise AssertionError("runaway sleep loop")
        if self.stop_at is not None and self.now + delay >= self.stop_at:
            self.now = max(self.now, self.stop_at)
            self.blocked.set()
            await asyncio.get_running_loop().create_future()  # parked until cancelled
        self.now += delay
        for hook in list(self.hooks):
            hook(self)
        await asyncio.sleep(0)


class FakeResponse:
    def __init__(self, status: int, payload: object, error: BaseException | None = None) -> None:
        self.status = status
        self._payload = payload
        self._error = error

    async def json(self) -> object:
        if self._error is not None:
            raise self._error
        return self._payload


def request_info(url: str = TOKEN_URL) -> RequestInfo:
    return RequestInfo(URL(url), "POST", CIMultiDictProxy(CIMultiDict()), URL(url))


def http_error(status: int, headers: dict[str, str] | None = None, url: str = TOKEN_URL) -> ClientResponseError:
    proxy = CIMultiDictProxy(CIMultiDict(headers or {}))
    return ClientResponseError(request_info(url), (), status=status, headers=proxy)


def connector_error() -> ClientConnectorError:
    key = mock.Mock(host="singlekey-id.com", port=443, ssl=True)
    return ClientConnectorError(key, OSError(111, "connection refused"))


def lock_is_held(lock_path: Path) -> bool:
    """True if another open file description holds the flock (works inside one process)."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


class FakeSession:
    """Token endpoint plus a data API that answers 401 when the bearer is expired by the fake clock.

    ``token_handler(session, kwargs)`` returns a ``FakeResponse`` or raises; it may be async.
    ``data_plan`` lists per-call overrides for data requests: ``"401"`` forces a rejection,
    ``("advance", seconds)`` moves the clock before the expiry check.
    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        token_handler: Callable[..., Any] | None = None,
        lifetime: float = ASSUMED_LIFETIME,
        request_seconds: float = 0.0,
        lock_path: Path | None = None,
    ) -> None:
        self.clock = clock
        self.lifetime = lifetime
        self.request_seconds = request_seconds
        self.lock_path = lock_path
        self.token_handler = token_handler
        self.calls: list[tuple[str, str, dict]] = []
        self.token_posts: list[dict] = []
        self.post_times: list[float] = []
        self.lock_held_during_post: list[bool] = []
        self.issued: list[tuple[str, str]] = []
        self.data_plan: list[Any] = []
        self.unauthorized = 0
        self.data_requests = 0

    def issue(self, kwargs: dict) -> FakeResponse:
        n = len(self.issued) + 1
        pair = (make_jwt(self.clock.now, self.lifetime), f"FAKE-refresh-issued-{n}")
        self.issued.append(pair)
        return FakeResponse(200, {"access_token": pair[0], "refresh_token": pair[1], "expires_in": self.lifetime})

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if url == TOKEN_URL:
            self.token_posts.append(dict(kwargs["data"]))
            self.post_times.append(self.clock.now)
            if self.lock_path is not None:
                self.lock_held_during_post.append(lock_is_held(self.lock_path))
            result = self.token_handler(self, kwargs) if self.token_handler else self.issue(kwargs)
            return await result if inspect.isawaitable(result) else result
        self.data_requests += 1
        step = self.data_plan.pop(0) if self.data_plan else None
        if isinstance(step, tuple) and step[0] == "advance":
            self.clock.now += step[1]
        self.clock.now += self.request_seconds
        bearer = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        try:
            exp = jwt_exp(bearer)
        except jwt.PyJWTError:
            exp = 0
        if step == "401" or exp <= self.clock.now:
            self.unauthorized += 1
            raise http_error(401, url=url)
        return FakeResponse(200, self.data(url))

    @staticmethod
    def data(url: str) -> object:
        if url.endswith("/resource/dhwCircuits"):
            return {"references": [{"id": f"/dhwCircuits/dhw{i}"} for i in (1, 2, 3)]}
        if url.endswith("/operationMode"):
            return {"value": "eco", "allowedValues": ["off", "eco", "comfort", "boost", "manual"]}
        return {"value": 1}

    def gaps(self) -> list[float]:
        """Seconds between consecutive token POSTs: the waits as the cloud sees them."""
        return [b - a for a, b in zip(self.post_times, self.post_times[1:])]

    def bearers(self) -> list[str]:
        return [kw["headers"]["Authorization"].removeprefix("Bearer ") for _, url, kw in self.calls if url != TOKEN_URL]


def write_auth_file(
    path: Path,
    generation: int,
    refresh: str,
    access: str | None,
    last_refresh_at: str | None = None,
    *,
    login_id: str | None = None,
    **extra: Any,
) -> None:
    """Simulate a write by another process (e.g. ``login``): atomic, mode 600."""
    tmp = path.with_name(".auth.json.test.tmp")
    data = {
        "refresh_token": refresh,
        "brand": "bosch",
        "generation": generation,
        "updated_at": "2026-10-04T12:00:00+00:00",
        "last_refresh_at": last_refresh_at,
        "access_token": access,
        "exp": jwt_exp(access) if access else None,
        "login_id": login_id,
        **extra,
    }
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def read_auth_file(path: Path) -> dict:
    return json.loads(path.read_text())


class ManagerFixture:
    """Builds a token manager on a temp ``auth.json`` with fake clock and recorded callbacks."""

    def __init__(self, directory: Path, *, lock_timeout: float = 30.0) -> None:
        self.dir = directory
        self.path = directory / "auth.json"
        self.store = AuthStore(self.path, lock_timeout=lock_timeout, poll_interval=0.01)
        self.clock = FakeClock()
        self.states: list[str] = []
        self.events: list[str] = []
        self.mounts = directory / "mounts"
        self.mounts.write_text("/dev/root / ext4 rw 0 0\n")

    def session(self, **kwargs: Any) -> FakeSession:
        return FakeSession(self.clock, lock_path=self.store.lock_path, **kwargs)

    def manager(self, session: FakeSession, *, poll_timeout: float = 300, store: AuthStore | None = None) -> TokenManager:
        return TokenManager(
            store or self.store,
            session,  # type: ignore[arg-type]
            brand="bosch",
            poll_timeout=poll_timeout,
            clock=self.clock.time,
            sleep=self.clock.sleep,
            on_state=self.states.append,
            on_error=lambda code, _message: self.events.append(code),
            mounts=str(self.mounts),
        )

    def seed(self, generation: int = 1, refresh: str = "FAKE-refresh-seed", *, remaining: float | None = None,
             lifetime: float = ASSUMED_LIFETIME, last_refresh_at: str | None = None) -> str | None:
        """Write ``auth.json``; with ``remaining`` an access token that expires that many seconds from now."""
        access = make_jwt(self.clock.now + remaining - lifetime, lifetime) if remaining is not None else None
        write_auth_file(self.path, generation, refresh, access, last_refresh_at)
        return access


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int = logging.DEBUG) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= level]
