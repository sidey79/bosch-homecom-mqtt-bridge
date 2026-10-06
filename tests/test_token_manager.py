import asyncio
import errno
import fcntl
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import jwt
from homecom_alt import ConnectionOptions, HomeComAlt
from homecom_alt.wddw2 import HomeComWddw2
from tenacity import wait_none

from bosch_homecom_mqtt_bridge.auth import store as store_module
from bosch_homecom_mqtt_bridge.auth import token_manager as tm_module
from bosch_homecom_mqtt_bridge.auth.claims import MAX_EXP_AHEAD, persistable_exp, token_times
from bosch_homecom_mqtt_bridge.auth.refresh_errors import RefreshClass
from bosch_homecom_mqtt_bridge.auth.token_manager import (
    AUTH_REQUIRED,
    DISCONNECTED,
    READY,
    STARTING,
    Phase,
    TokenManagerClosedError,
    network_filesystem,
)

from .token_fakes import (
    ASSUMED_LIFETIME,
    DEVICE,
    JWT_KEY,
    TOKEN_URL,
    FakeResponse,
    ListHandler,
    ManagerFixture,
    connector_error,
    http_error,
    make_jwt,
    read_auth_file,
    write_auth_file,
)


class ManagerTestCase(unittest.IsolatedAsyncioTestCase):
    lock_timeout = 30.0

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ManagerFixture(Path(self._tmp.name), lock_timeout=self.lock_timeout)
        self.clock = self.fx.clock
        self.log = ListHandler()
        logger = logging.getLogger("bosch_homecom_mqtt_bridge")
        self._saved_level = logger.level
        logger.addHandler(self.log)
        logger.setLevel(logging.DEBUG)

    async def asyncTearDown(self) -> None:
        logger = logging.getLogger("bosch_homecom_mqtt_bridge")
        logger.removeHandler(self.log)
        logger.setLevel(self._saved_level)
        self._tmp.cleanup()

    def wddw2(self, manager) -> HomeComWddw2:
        return manager.fetch_api(HomeComWddw2, device_id=DEVICE)

    async def park(self, call) -> asyncio.Task:
        """Run ``call`` until it ends or the fake clock parks (``stop_at``)."""
        caller = asyncio.create_task(call)
        blocked = asyncio.create_task(self.clock.blocked.wait())
        await asyncio.wait({caller, blocked}, return_when=asyncio.FIRST_COMPLETED)
        blocked.cancel()
        return caller

    async def stop(self, manager, caller: asyncio.Task) -> None:
        """Kill the process: close the manager and collect the caller."""
        await manager.aclose()
        await asyncio.wait({caller})
        if not caller.cancelled():
            caller.exception()

    def restart(self, stop_at: float | None = None) -> None:
        self.clock.stop_at = stop_at
        self.clock.blocked = asyncio.Event()


def reject(status: int):
    def handler(session, kwargs):
        raise http_error(status)

    return handler


class RuntimeIndependenceTest(ManagerTestCase):
    async def test_token_expiring_mid_poll_costs_one_401_one_locked_refresh_one_repeat(self) -> None:
        # Fake clock: 15 s per request. 3 circuits with 4 levels each: 7 + 3 * (8 + 4) + 1 = 44 requests.
        # The token has 400 s left (margin min(300 + 60, 3600 / 2) = 360), so no refresh before the
        # poll; it expires during request 27.
        self.fx.seed(remaining=400)
        session = self.fx.session(request_seconds=15)
        manager = self.fx.manager(session)
        api = self.wddw2(manager)
        polls = 0

        async def poll():
            nonlocal polls
            polls += 1
            return await api.async_update(DEVICE)

        with mock.patch.object(manager._refresh_api, "get_token", wraps=manager._refresh_api.get_token) as refresh:
            result = await manager.run_poll(poll)

        self.assertEqual(result.device, DEVICE)
        self.assertEqual(len(result.dhw_circuits), 3)
        self.assertEqual(session.unauthorized, 1)
        self.assertEqual(polls, 2)
        self.assertEqual(session.data_requests, 27 + 44)
        # Exactly one call to the token endpoint, made by the refresh instance under the lock;
        # the fetch instance made none.
        self.assertEqual([url for _, url, _ in session.calls].count(TOKEN_URL), 1)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(refresh.call_args.kwargs, {"force": True})
        self.assertEqual(session.lock_held_during_post, [True])
        self.assertEqual(read_auth_file(self.fx.path)["generation"], 2)

    async def test_fetch_instance_never_refreshes_even_with_expired_token_refresh_token_and_code(self) -> None:
        # Protection against library updates, part 1, with a prescribed state.
        expired = make_jwt(self.clock.now - 2 * ASSUMED_LIFETIME, ASSUMED_LIFETIME)
        options = ConnectionOptions(token=expired, refresh_token="FAKE-refresh-keep", code="FAKE-code-keep")
        session = self.fx.session()
        session.data_plan = []
        api = HomeComWddw2(session, options, DEVICE, auth_provider=False)  # type: ignore[arg-type]

        self.assertIsNone(await api.get_token())
        with self.assertRaises(Exception):
            await api.async_update(DEVICE)  # 401 with the expired bearer, no refresh
        with mock.patch.object(HomeComAlt.async_get_devices.retry, "wait", wait_none()), self.assertRaises(Exception):
            await api.async_get_devices()
        self.assertEqual(session.token_posts, [])
        self.assertNotIn(TOKEN_URL, [url for _, url, _ in session.calls])
        self.assertEqual((options.refresh_token, options.code, options.token), ("FAKE-refresh-keep", "FAKE-code-keep", expired))

        # The factory builds fetch instances exactly like that, on the shared options.
        manager = self.fx.manager(session)
        fetch = self.wddw2(manager)
        self.assertFalse(fetch._auth_provider)
        self.assertIs(fetch._options, manager._options)
        self.assertIs(manager._refresh_api._options, manager._options)

    async def test_refresh_instance_refreshes_although_options_auth_provider_is_false(self) -> None:
        # Protection part 2: only the constructor flag matters; ConnectionOptions.auth_provider is
        # unused in homecom_alt 1.8.2 and the factory sets it to False explicitly.
        session = self.fx.session()
        manager = self.fx.manager(session)
        self.assertIs(manager._options.auth_provider, False)
        self.assertTrue(manager._refresh_api._auth_provider)
        options = ConnectionOptions(refresh_token="FAKE-refresh-before", auth_provider=False)
        api = HomeComAlt(session, options, auth_provider=True)  # type: ignore[arg-type]
        self.assertIs(await api.get_token(force=True), True)
        self.assertEqual(options.refresh_token, "FAKE-refresh-issued-1")
        self.assertEqual(session.token_posts[0]["refresh_token"], "FAKE-refresh-before")


class GenerationTest(ManagerTestCase):
    async def test_login_wins_without_post(self) -> None:
        self.fx.seed(generation=4, refresh="FAKE-refresh-old", remaining=-10)
        session = self.fx.session()
        manager = self.fx.manager(session)
        manager._adopt(await self.fx.store.read_locked())
        login_access = make_jwt(self.clock.now, ASSUMED_LIFETIME)
        write_auth_file(self.fx.path, 5, "FAKE-refresh-login", login_access)

        await manager.refresh_locked()

        self.assertEqual(session.token_posts, [])
        self.assertEqual((manager._options.token, manager._options.refresh_token), (login_access, "FAKE-refresh-login"))
        self.assertEqual(read_auth_file(self.fx.path)["generation"], 5)

    async def test_login_without_access_token_is_refreshed_with_the_adopted_token(self) -> None:
        self.fx.seed(generation=4, refresh="FAKE-refresh-old", remaining=-10)
        session = self.fx.session()
        manager = self.fx.manager(session)
        manager._adopt(await self.fx.store.read_locked())
        write_auth_file(self.fx.path, 5, "FAKE-refresh-login", None)

        await manager.refresh_locked()

        self.assertEqual([body["refresh_token"] for body in session.token_posts], ["FAKE-refresh-login"])
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"]), (6, "FAKE-refresh-issued-1"))

    async def test_every_ensure_fresh_reads_the_generation_and_adopts_a_login_before_the_first_request(self) -> None:
        first = self.fx.seed(generation=1, remaining=3000)
        session = self.fx.session()
        manager = self.fx.manager(session)
        api = manager.fetch_api()
        with mock.patch.object(self.fx.store, "read_locked", wraps=self.fx.store.read_locked) as reads:
            await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
            login_access = make_jwt(self.clock.now, ASSUMED_LIFETIME)
            write_auth_file(self.fx.path, 2, "FAKE-refresh-login", login_access)
            await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
            await manager.ensure_fresh()
        self.assertEqual(reads.call_count, 3)
        self.assertEqual(session.bearers(), [first, login_access])
        self.assertEqual(session.token_posts, [])

    async def test_missing_access_token_is_refreshed_before_the_first_request(self) -> None:
        self.fx.seed(refresh="FAKE-refresh-seed", remaining=None)
        session = self.fx.session()
        manager = self.fx.manager(session)
        self.assertIsNone(manager._options.token)
        api = manager.fetch_api()
        await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
        self.assertEqual([url for _, url, _ in session.calls][0], TOKEN_URL)
        self.assertEqual(session.token_posts[0]["refresh_token"], "FAKE-refresh-seed")
        self.assertEqual(session.bearers(), [session.issued[0][0]])
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["access_token"], saved["exp"]), (session.issued[0][0], int(self.clock.now) + ASSUMED_LIFETIME))
        self.assertIsNotNone(saved["last_refresh_at"])
        self.assertEqual(manager.state, READY)

    async def test_no_auth_file_means_auth_required_without_requests(self) -> None:
        session = self.fx.session()
        manager = self.fx.manager(session)
        self.clock.stop_at = self.clock.now + 35
        caller = asyncio.create_task(manager.ensure_fresh())
        await self.clock.blocked.wait()
        self.assertEqual(manager.state, AUTH_REQUIRED)
        self.assertEqual(self.fx.events, ["AUTH_REQUIRED"])
        self.assertEqual(session.calls, [])
        await manager.aclose()
        with self.assertRaises(TokenManagerClosedError):
            await caller


class MarginTest(ManagerTestCase):
    async def test_short_lifetime_refreshes_far_less_than_once_per_poll_and_warns_once(self) -> None:
        # m4: no request time here. Lifetime 300 s, poll timeout 300 s, interval 60 s, 1 h.
        self.fx.seed(remaining=300, lifetime=300)
        session = self.fx.session(lifetime=300)
        manager = self.fx.manager(session, poll_timeout=300)
        api = manager.fetch_api()
        for _ in range(60):
            await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
            await self.clock.sleep(60)
        self.assertEqual(session.unauthorized, 0)
        self.assertLessEqual(len(session.token_posts), 25)
        self.assertGreaterEqual(len(session.token_posts), 1)
        warnings = [m for m in self.log.messages(logging.WARNING) if "exceeds half the token lifetime" in m]
        self.assertEqual(len(warnings), 1)

    async def test_assumed_lifetime_keeps_full_margin_without_warning(self) -> None:
        self.fx.seed(remaining=361)
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(session.token_posts, [])
        self.clock.now += 2
        await manager.ensure_fresh()  # 359 s left < 360 s margin
        self.assertEqual(len(session.token_posts), 1)
        self.assertFalse([m for m in self.log.messages() if "exceeds half" in m])


class AuthRequiredTest(ManagerTestCase):
    async def test_leaves_auth_required_within_10_s_of_a_new_generation_without_requests_before(self) -> None:
        self.fx.seed(generation=3, remaining=-1)

        def reject(session, kwargs):
            raise http_error(400)

        session = self.fx.session(token_handler=reject)
        manager = self.fx.manager(session)
        written_at = {}
        login_access = make_jwt(self.clock.now + 95, ASSUMED_LIFETIME)

        def login(clock):
            if not written_at and clock.now >= self.clock_start + 95:
                write_auth_file(self.fx.path, 4, "FAKE-refresh-login", login_access)
                written_at["t"] = clock.now
                written_at["calls"] = len(session.calls)

        self.clock_start = self.clock.now
        self.clock.hooks.append(login)
        await manager.ensure_fresh()

        self.assertEqual(manager.state, READY)
        self.assertEqual(self.fx.states, [AUTH_REQUIRED, READY])
        self.assertEqual(self.fx.events, ["AUTH_REQUIRED"])
        self.assertLessEqual(self.clock.now - written_at["t"], 10)
        self.assertEqual(written_at["calls"], 1)  # only the rejected POST before the login
        self.assertEqual(len(session.calls), 1)  # the adopted login token needs no POST
        self.assertEqual(manager._options.token, login_access)


class CrashLoopTest(ManagerTestCase):
    async def restart_loop(self, session) -> int:
        start = self.clock.now
        for i in range(20):  # 20 restarts in 10 min: every 30 s a new process
            self.clock.now = max(self.clock.now, start + 30 * i)
            self.clock.stop_at = start + 30 * (i + 1)
            self.clock.blocked = asyncio.Event()
            manager = self.fx.manager(session)
            run = asyncio.create_task(manager.ensure_fresh())
            blocked = asyncio.create_task(self.clock.blocked.wait())
            await asyncio.wait({run, blocked}, return_when=asyncio.FIRST_COMPLETED)
            blocked.cancel()
            await manager.aclose()  # the next restart kills this process
            if not run.done():
                run.cancel()
            await asyncio.wait({run})
            if not run.cancelled():
                run.exception()  # TokenManagerClosedError when killed while waiting
        self.assertLessEqual(self.clock.now, start + 600)
        return len(session.token_posts)

    async def test_twenty_restarts_in_ten_minutes_refresh_at_most_eleven_times(self) -> None:
        # Tokens live 20 s, so every restart needs a refresh; the guard keeps them 60 s apart.
        self.fx.seed(remaining=None)
        session = self.fx.session(lifetime=20)
        posts = await self.restart_loop(session)
        self.assertLessEqual(posts, 11)
        self.assertGreaterEqual(posts, 9)  # the guard delays refreshes, it does not suppress them

    async def test_valid_access_token_after_login_needs_no_refresh_and_no_wait(self) -> None:
        # D7 = yes and m9: login wrote a valid access token and no last_refresh_at; a service
        # refresh 10 s ago is carried over in last_refresh_at.
        recent = tm_module._iso(self.clock.now - 10)
        self.fx.seed(remaining=ASSUMED_LIFETIME - 5, last_refresh_at=recent)
        session = self.fx.session()
        posts = await self.restart_loop(session)
        self.assertEqual(posts, 0)
        self.assertEqual(self.clock.sleeps, [])


class AbortTest(ManagerTestCase):
    async def test_cancelled_caller_does_not_abort_the_post(self) -> None:
        self.fx.seed(remaining=-1)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(session, kwargs):
            started.set()
            await release.wait()
            self.clock.now += 10  # 10 s POST
            return session.issue(kwargs)

        session = self.fx.session(token_handler=slow)
        manager = self.fx.manager(session)
        caller = asyncio.create_task(manager.ensure_fresh())
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(manager.phase, Phase.SENDING)
        release.set()
        await asyncio.wait({manager._task})
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"]), (2, "FAKE-refresh-issued-1"))

    async def test_aclose_during_lock_acquisition_sends_nothing(self) -> None:
        self.fx.seed(remaining=-1)
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()  # adopt generation 1 first; no lock contention yet
        self.clock.now += ASSUMED_LIFETIME
        fd = os.open(self.fx.store.lock_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            posts_before = len(session.token_posts)
            caller = asyncio.create_task(manager.refresh_locked())
            while manager.phase is not Phase.ACQUIRING:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await manager.aclose()
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            os.close(fd)
        with self.assertRaises(TokenManagerClosedError):
            await caller
        self.assertEqual(len(session.token_posts), posts_before)
        with self.assertRaises(TokenManagerClosedError):
            await manager.ensure_fresh()

    async def test_aclose_during_post_returns_only_after_the_write(self) -> None:
        self.fx.seed(remaining=-1)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(session, kwargs):
            started.set()
            await release.wait()
            return session.issue(kwargs)

        session = self.fx.session(token_handler=slow)
        manager = self.fx.manager(session)
        caller = asyncio.create_task(manager.ensure_fresh())
        await started.wait()
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0.05)
        self.assertFalse(closing.done())
        release.set()
        await closing
        await caller
        self.assertEqual(read_auth_file(self.fx.path)["refresh_token"], "FAKE-refresh-issued-1")

    async def test_aclose_during_backoff_ends_quickly_without_further_posts(self) -> None:
        self.fx.seed(remaining=-1)

        def unreachable(session, kwargs):
            raise connector_error()  # wrapped by the library as NotRespondingError (K4)

        session = self.fx.session(token_handler=unreachable)
        manager = self.fx.manager(session)
        self.clock.stop_at = self.clock.now + 1  # park the first backoff (30 s)
        caller = asyncio.create_task(manager.ensure_fresh())
        await self.clock.blocked.wait()
        self.assertEqual((manager.phase, manager.state), (Phase.BACKOFF, DISCONNECTED))
        posts = len(session.token_posts)
        started = time.monotonic()
        await manager.aclose()
        self.assertLess(time.monotonic() - started, 1.0)
        with self.assertRaises(TokenManagerClosedError):
            await caller
        await asyncio.sleep(0.05)
        self.assertEqual(len(session.token_posts), posts)

    async def test_poll_timeout_never_wraps_the_refresh(self) -> None:
        self.fx.seed(remaining=3000)

        async def slow(session, kwargs):
            await asyncio.sleep(0.5)  # longer than the poll timeout below (real time)
            return session.issue(kwargs)

        session = self.fx.session(token_handler=slow)
        session.data_plan = ["401"]
        manager = self.fx.manager(session, poll_timeout=0.2)
        api = manager.fetch_api()
        await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
        self.assertEqual(len(session.token_posts), 1)

        async def slow_poll():
            await asyncio.sleep(0.5)

        with self.assertRaises(TimeoutError):
            await manager.run_poll(slow_poll)


class SecondUnauthorizedTest(ManagerTestCase):
    async def test_second_401_with_token_valid_by_exp_means_auth_required(self) -> None:
        self.fx.seed(generation=1, remaining=3000)
        session = self.fx.session()
        session.data_plan = ["401", "401"]
        manager = self.fx.manager(session)
        api = manager.fetch_api()
        login_access = make_jwt(self.clock.now, ASSUMED_LIFETIME)
        start = self.clock.now

        def login(clock):
            if clock.now >= start + 20 and read_auth_file(self.fx.path)["generation"] < 3:
                write_auth_file(self.fx.path, 3, "FAKE-refresh-login", login_access)

        self.clock.hooks.append(login)
        await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
        self.assertEqual(self.fx.events, ["AUTH_REQUIRED"])
        self.assertEqual(len(session.token_posts), 1)
        self.assertEqual(session.bearers()[-1], login_access)
        self.assertEqual(session.data_requests, 3)

    async def test_second_401_with_expired_token_runs_ensure_fresh_once_more(self) -> None:
        self.fx.seed(generation=1, remaining=3000)
        session = self.fx.session()
        # 1: rejected; refresh; 2: the clock jumps past exp of the new token; ensure_fresh; 3: ok.
        session.data_plan = ["401", ("advance", ASSUMED_LIFETIME + 1)]
        manager = self.fx.manager(session)
        api = manager.fetch_api()
        await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
        self.assertEqual(self.fx.events, [])
        self.assertEqual(len(session.token_posts), 2)
        self.assertEqual(session.data_requests, 3)
        self.assertEqual(manager.state, READY)


class PersistenceFailureTest(ManagerTestCase):
    async def test_write_failure_after_refresh_keeps_tokens_in_memory_and_retries_before_each_poll(self) -> None:
        # M1 with a fake file system that is full (ENOSPC on rename).
        self.fx.seed(generation=2, refresh="FAKE-refresh-seed", remaining=-1)
        before = self.fx.path.read_bytes()
        session = self.fx.session()
        manager = self.fx.manager(session)
        api = manager.fetch_api()
        full = OSError(errno.ENOSPC, "FAKE-strerror")
        with mock.patch.object(store_module.os, "replace", side_effect=full):
            await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
            self.assertEqual(manager.state, READY)
            self.assertEqual(self.fx.path.read_bytes(), before)
            self.assertEqual(manager._options.refresh_token, "FAKE-refresh-issued-1")
            errors = self.log.messages(logging.ERROR)
            self.assertTrue(any("could not be written" in m and "ENOSPC" in m for m in errors), errors)
            for _ in range(3):
                self.clock.now += 60
                await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
            self.assertEqual(len(session.token_posts), 1)  # no further refresh while unpersisted
            self.assertEqual(sum("still not written" in m for m in self.log.messages(logging.ERROR)), 3)
            # The access token becomes due: a refresh is allowed, with the newest token in memory.
            self.clock.now += ASSUMED_LIFETIME
            await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
            self.assertEqual([b["refresh_token"] for b in session.token_posts], ["FAKE-refresh-seed", "FAKE-refresh-issued-1"])
            self.assertEqual(self.fx.path.read_bytes(), before)
        await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"]), (3, "FAKE-refresh-issued-2"))
        self.assertEqual(saved["access_token"], session.issued[1][0])
        self.assertIsNotNone(saved["last_refresh_at"])
        self.assertEqual(len(session.token_posts), 2)
        self.assertNotIn("FAKE-strerror", "\n".join(self.log.messages()))

    async def test_login_during_unpersisted_state_wins(self) -> None:
        self.fx.seed(generation=2, remaining=-1)
        session = self.fx.session()
        manager = self.fx.manager(session)
        with mock.patch.object(store_module.os, "replace", side_effect=OSError(errno.ENOSPC, "x")):
            await manager.ensure_fresh()
        login_access = make_jwt(self.clock.now, ASSUMED_LIFETIME)
        write_auth_file(self.fx.path, 3, "FAKE-refresh-login", login_access)
        await manager.ensure_fresh()
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"]), (3, "FAKE-refresh-login"))
        self.assertEqual(manager._options.token, login_access)
        self.assertIsNone(manager._unpersisted)


class InProcessLockTest(ManagerTestCase):
    lock_timeout = 0.3

    async def test_ensure_fresh_joins_the_running_refresh_instead_of_locking_itself_out(self) -> None:
        # m3: the refresh task holds the store lock for longer than its timeout; a concurrent
        # ensure_fresh() must not wait for the lock (it would time out) but join the task.
        self.fx.seed(remaining=-1)

        async def slow(session, kwargs):
            await asyncio.sleep(0.6)
            return session.issue(kwargs)

        session = self.fx.session(token_handler=slow)
        manager = self.fx.manager(session)
        first = asyncio.create_task(manager.ensure_fresh())
        while manager.phase is not Phase.SENDING:
            await asyncio.sleep(0.01)
        await asyncio.gather(first, manager.ensure_fresh(), manager.ensure_fresh())
        self.assertEqual(len(session.token_posts), 1)
        self.assertEqual(manager.state, READY)
        self.assertFalse([m for m in self.log.messages() if "Reading the auth file failed" in m])
        self.assertEqual((await self.fx.store.read_locked()).generation, 2)


class FilesystemWarningTest(ManagerTestCase):
    async def test_nfs_and_cifs_are_detected_by_the_longest_mount_point(self) -> None:
        data = self.fx.dir / "with space"
        data.mkdir()
        escaped = str(data).replace(" ", "\\040")
        mounts = self.fx.dir / "mounts-net"
        network = ("nfs4", "cifs", "smb", "smb3", "ceph", "glusterfs", "9p", "virtiofs", "afs", "fuse.sshfs", "fuseblk")
        for fs_type, expected in (*((t, t) for t in network), ("ext4", None), ("overlay", None), ("tmpfs", None)):
            with self.subTest(fs_type=fs_type):
                mounts.write_text(f"/dev/root / ext4 rw 0 0\nserver:/x {escaped} {fs_type} rw 0 0\n")
                self.assertEqual(network_filesystem(data / "auth.json", str(mounts)), expected)
        self.assertIsNone(network_filesystem(data / "auth.json", str(self.fx.dir / "missing")))

    async def test_manager_warns_once_on_network_filesystem(self) -> None:
        self.fx.mounts.write_text(f"/dev/root / ext4 rw 0 0\nserver:/x {self.fx.dir} nfs rw 0 0\n")
        self.fx.manager(self.fx.session())
        warnings = [m for m in self.log.messages(logging.WARNING) if "flock is only reliable" in m]
        self.assertEqual(len(warnings), 1)


class RedactionTest(ManagerTestCase):
    async def test_new_tokens_are_registered_and_never_logged(self) -> None:
        from bosch_homecom_mqtt_bridge.logging_setup import RedactionFilter

        redaction = RedactionFilter()
        root = logging.getLogger()
        handler = ListHandler()
        handler.addFilter(redaction)
        root.addHandler(handler)
        try:
            self.fx.seed(refresh="FAKE-refresh-seed", remaining=-1)
            session = self.fx.session()
            manager = self.fx.manager(session)
            await manager.ensure_fresh()
            access, refresh = session.issued[0]
            logging.getLogger("test.token_manager.redaction").warning("%s|%s|%s", access, refresh, "FAKE-refresh-seed")
        finally:
            root.removeHandler(handler)
        self.assertIn("***|***|***", handler.messages())
        for secret in (access, refresh, "FAKE-refresh-seed"):
            self.assertNotIn(secret, "\n".join(handler.messages()))
            self.assertNotIn(secret, "\n".join(self.log.messages()))


class StoreReadFailureTest(ManagerTestCase):
    async def test_unreadable_file_with_fresh_token_in_memory_keeps_polling(self) -> None:
        self.fx.seed(remaining=3000)
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.fx.path.write_text("not json")
        api = manager.fetch_api()
        await manager.run_poll(lambda: api.async_get_notifications(DEVICE))
        self.assertEqual(session.token_posts, [])
        self.assertTrue(any("Reading the auth file failed" in m for m in self.log.messages(logging.WARNING)))

    async def test_invalid_file_when_refresh_is_due_means_auth_required(self) -> None:
        self.fx.path.write_text("not json")
        session = self.fx.session()
        manager = self.fx.manager(session)
        self.clock.stop_at = self.clock.now + 5
        caller = asyncio.create_task(manager.ensure_fresh())
        await self.clock.blocked.wait()
        self.assertEqual(manager.state, AUTH_REQUIRED)
        self.assertEqual(session.calls, [])
        await manager.aclose()
        with self.assertRaises(TokenManagerClosedError):
            await caller


class RestartSafetyTest(ManagerTestCase):
    """Decision (B): auth_required and the K3 limit survive a restart (ADR 0001)."""

    async def test_auth_required_survives_a_restart_without_a_post_until_a_login(self) -> None:
        self.fx.seed(generation=2, remaining=-1)
        session = self.fx.session(token_handler=reject(401))
        first = self.fx.manager(session)
        self.clock.stop_at = self.clock.now + 5
        caller = await self.park(first.ensure_fresh())
        self.assertEqual(first.state, AUTH_REQUIRED)
        await self.stop(first, caller)
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_blocked"], saved["blocked_generation"]), (2, "K2", 2))

        self.restart()
        start = self.clock.now
        login_access = make_jwt(start, ASSUMED_LIFETIME)

        def login(clock):
            if clock.now >= start + 25 and read_auth_file(self.fx.path)["generation"] == 2:
                write_auth_file(self.fx.path, 3, "FAKE-refresh-login", login_access, login_id="b0b0")

        self.clock.hooks.append(login)
        second = self.fx.manager(session)
        await second.ensure_fresh()
        self.assertEqual(len(session.token_posts), 1)  # the restart sent nothing
        self.assertEqual(self.fx.states, [AUTH_REQUIRED, AUTH_REQUIRED, READY])
        self.assertEqual(self.fx.events, ["AUTH_REQUIRED", "AUTH_REQUIRED"])
        self.assertEqual(second._options.token, login_access)

    async def test_block_that_cannot_be_written_is_not_retried_like_k4_and_written_while_waiting(self) -> None:
        self.fx.seed(generation=2, remaining=-1)
        session = self.fx.session(token_handler=reject(400))
        manager = self.fx.manager(session)
        start = self.clock.now
        self.clock.stop_at = start + 25
        real_replace = os.replace
        with mock.patch.object(store_module.os, "replace", side_effect=OSError(errno.ENOSPC, "x")) as replace:

            def space_back(clock):
                if clock.now >= start + 15:
                    replace.side_effect = real_replace

            self.clock.hooks.append(space_back)
            caller = await self.park(manager.ensure_fresh())
        self.assertEqual(self.fx.states, [AUTH_REQUIRED])  # never disconnected/backoff: the POST was sent
        self.assertEqual(len(session.token_posts), 1)
        self.assertTrue(any("Refresh state could not be written" in m for m in self.log.messages(logging.ERROR)))
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_blocked"], saved["blocked_generation"]), (2, "K1", 2))
        await self.stop(manager, caller)

        self.restart(stop_at=self.clock.now + 5)
        self.clock.hooks.clear()
        restarted = self.fx.manager(session)
        caller = await self.park(restarted.ensure_fresh())
        self.assertEqual(restarted.state, AUTH_REQUIRED)
        self.assertEqual(len(session.token_posts), 1)
        await self.stop(restarted, caller)

    async def test_unwritten_k3_marks_never_overwrite_a_later_block(self) -> None:
        self.fx.seed(generation=2, remaining=-1)
        answers = [http_error(429, {"Retry-After": "0"}), http_error(401)]

        def handler(session, kwargs):
            raise answers.pop(0)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        start = self.clock.now
        self.clock.stop_at = start + 85
        real_replace = os.replace
        with mock.patch.object(store_module.os, "replace", side_effect=OSError(errno.ENOSPC, "x")) as replace:

            def space_back(clock):
                if clock.now >= start + 30:
                    replace.side_effect = real_replace

            self.clock.hooks.append(space_back)
            caller = await self.park(manager.ensure_fresh())
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["refresh_blocked"], saved["blocked_generation"]), ("K2", 2))
        self.assertEqual(session.gaps(), [60])
        self.assertEqual(set(self.clock.sleeps), {10})  # the K3 wait runs in 10-s steps
        await self.stop(manager, caller)

    async def test_k3_wait_and_post_limit_survive_a_restart(self) -> None:
        self.fx.seed(remaining=-1)
        start = self.clock.now
        posted_at: list[float] = []

        def handler(session, kwargs):
            posted_at.append(session.clock.now - start)
            if len(posted_at) <= 3:
                raise http_error(429, {"Retry-After": "0"})
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        first = self.fx.manager(session)
        self.clock.stop_at = start + 200  # the third 429 (t = 120 s) waits for the window; killed meanwhile
        caller = await self.park(first.ensure_fresh())
        await self.stop(first, caller)
        saved = read_auth_file(self.fx.path)
        self.assertEqual(saved["not_before"], start + 3600)
        self.assertEqual(saved["refresh_posts"], [start, start + 60, start + 120])

        self.restart()
        self.clock.now = start + 300
        second = self.fx.manager(session)
        await second.ensure_fresh()
        self.assertEqual(posted_at, [0, 60, 120, 3600])
        self.assertEqual(session.gaps()[-1], 3600 - 120)
        self.assertLessEqual(max(self.clock.sleeps), 10)
        self.assertEqual(second.state, READY)
        self.assertEqual(read_auth_file(self.fx.path)["not_before"], None)


class FileIdentityTest(ManagerTestCase):
    """MAJOR-3: adopt the file when generation, login_id or refresh token differ, not only when higher."""

    async def test_new_login_after_deleting_the_file_is_adopted_with_a_lower_generation(self) -> None:
        self.fx.seed(generation=5, remaining=-1)
        session = self.fx.session(token_handler=reject(400))
        manager = self.fx.manager(session)
        start = self.clock.now
        login_access = make_jwt(start, ASSUMED_LIFETIME)

        def relogin(clock):
            if clock.now >= start + 25 and read_auth_file(self.fx.path)["generation"] == 5:
                self.fx.path.unlink()
                write_auth_file(self.fx.path, 1, "FAKE-refresh-relogin", login_access, login_id="c0c0")

        self.clock.hooks.append(relogin)
        self.clock.stop_at = start + 100
        caller = await self.park(manager.ensure_fresh())
        try:
            self.assertTrue(caller.done())
            self.assertEqual(manager.state, READY)
            self.assertEqual(manager._options.token, login_access)
            self.assertEqual(len(session.token_posts), 1)
        finally:
            await self.stop(manager, caller)

    async def test_restored_backup_with_a_lower_generation_is_adopted(self) -> None:
        backup_access = self.fx.seed(generation=2, refresh="FAKE-refresh-backup", remaining=3000)
        backup = self.fx.path.read_bytes()
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        await manager.refresh_locked(rejected_token=backup_access)
        self.assertEqual(read_auth_file(self.fx.path)["generation"], 3)
        self.fx.path.write_bytes(backup)  # the operator restores the backup
        await manager.ensure_fresh()
        self.assertEqual((manager._options.token, manager._options.refresh_token), (backup_access, "FAKE-refresh-backup"))
        self.assertEqual(len(session.token_posts), 1)


class ReviewFindingsTest(ManagerTestCase):
    async def test_stale_pending_write_never_runs_after_a_newer_one(self) -> None:
        # MAJOR-2: M1, then two ensure_fresh() and a 401 while another process holds the lock.
        self.fx.seed(generation=2, remaining=-1)
        session = self.fx.session()
        manager = self.fx.manager(session)
        with mock.patch.object(store_module.os, "replace", side_effect=OSError(errno.ENOSPC, "x")):
            await manager.ensure_fresh()
        fd = os.open(self.fx.store.lock_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            first = asyncio.create_task(manager.ensure_fresh())
            second = asyncio.create_task(manager.ensure_fresh())
            unauthorized = asyncio.create_task(manager.on_unauthorized(manager._options.token))
            await asyncio.sleep(0.05)
        finally:
            os.close(fd)
        await asyncio.gather(first, second, unauthorized)
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"]), (4, "FAKE-refresh-issued-2"))
        self.assertEqual([b["refresh_token"] for b in session.token_posts], ["FAKE-refresh-seed", "FAKE-refresh-issued-1"])

    async def test_store_validation_error_after_refresh_keeps_the_tokens_like_m1(self) -> None:
        self.fx.seed(generation=2, remaining=-1)
        session = self.fx.session()
        manager = self.fx.manager(session)
        real, refused = store_module._valid_access, []

        def refuse_the_write(access, exp):
            if session.issued and access == session.issued[0][0] and not refused:
                refused.append(access)  # only the first write of the new tokens
                return False
            return real(access, exp)

        with mock.patch.object(store_module, "_valid_access", side_effect=refuse_the_write):
            await manager.ensure_fresh()
        self.assertEqual(manager.state, READY)
        self.assertEqual(manager._unpersisted.refresh_token, "FAKE-refresh-issued-1")
        self.assertTrue(any("could not be written" in m and "refusing" in m for m in self.log.messages(logging.ERROR)))
        await manager.ensure_fresh()
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"]), (3, "FAKE-refresh-issued-1"))
        self.assertEqual(len(session.token_posts), 1)

    async def test_tokens_are_secured_before_the_claims_are_parsed(self) -> None:
        self.fx.seed(generation=2, remaining=-1)
        session = self.fx.session()
        manager = self.fx.manager(session)
        with mock.patch.object(tm_module, "persistable_exp", side_effect=RuntimeError("FAKE-detail")):
            with self.assertRaises(RuntimeError):
                await manager.ensure_fresh()
        await asyncio.sleep(0)
        self.assertEqual(manager._unpersisted.refresh_token, "FAKE-refresh-issued-1")
        # Done callback: the task's failure is retrieved and logged with its type only.
        self.assertIn("Refresh task failed: RuntimeError", self.log.messages(logging.ERROR))
        self.assertFalse([m for m in self.log.messages() if "FAKE-detail" in m])
        await manager.ensure_fresh()
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"]), (3, "FAKE-refresh-issued-1"))
        self.assertEqual(len(session.token_posts), 1)

    async def test_unusable_token_type_is_k9_rolled_back_and_logged_by_type(self) -> None:
        self.fx.seed(generation=2, refresh="FAKE-refresh-seed", remaining=-1)

        def numeric(session, kwargs):
            return FakeResponse(200, {"access_token": 12345, "refresh_token": "FAKE-refresh-new"})

        session = self.fx.session(token_handler=numeric)
        manager = self.fx.manager(session)
        self.clock.stop_at = self.clock.now + 5
        caller = await self.park(manager.ensure_fresh())
        self.assertEqual((manager.state, manager.last_failure), (AUTH_REQUIRED, RefreshClass.K9))
        self.assertEqual(manager._options.refresh_token, "FAKE-refresh-seed")
        self.assertTrue(any("K9" in m and m.endswith(", int") for m in self.log.messages(logging.ERROR)))
        await self.stop(manager, caller)

    async def test_unchanged_refresh_token_keeps_it_with_the_new_access_token(self) -> None:
        self.fx.seed(generation=2, refresh="FAKE-refresh-seed", remaining=-1)
        new_access = make_jwt(self.clock.now, ASSUMED_LIFETIME)

        def no_rotation(session, kwargs):
            return FakeResponse(200, {"access_token": new_access, "refresh_token": "FAKE-refresh-seed"})

        session = self.fx.session(token_handler=no_rotation)
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual((manager.state, self.fx.events), (READY, []))
        saved = read_auth_file(self.fx.path)
        self.assertEqual((saved["generation"], saved["refresh_token"], saved["access_token"]), (3, "FAKE-refresh-seed", new_access))
        self.assertTrue(any("no rotation" in m for m in self.log.messages(logging.WARNING)))

    async def test_crash_loop_guard_waits_at_most_60_s_for_a_future_last_refresh(self) -> None:
        self.fx.seed(remaining=None, last_refresh_at=tm_module._iso(self.clock.now + 86400))
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(self.clock.sleeps, [60])
        self.assertEqual(len(session.token_posts), 1)

    async def test_crash_loop_guard_also_holds_for_a_first_refresh_without_a_prior_read(self) -> None:
        self.fx.seed(remaining=None, last_refresh_at=tm_module._iso(self.clock.now - 10))
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.refresh_locked()
        self.assertEqual(self.clock.sleeps, [50])
        self.assertEqual(len(session.token_posts), 1)

    async def test_persisted_exp_is_the_fallback_for_an_unreadable_access_token(self) -> None:
        data = {
            "refresh_token": "FAKE-refresh-seed",
            "brand": "bosch",
            "generation": 1,
            "updated_at": "2020-09-13T12:26:40+00:00",
            "last_refresh_at": None,
            "access_token": "FAKE-access-opaque-0123456789",
            "exp": int(self.clock.now) + 3000,
        }
        self.fx.path.write_text(json.dumps(data))
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual((manager.state, session.token_posts), (READY, []))
        self.clock.now += 2700  # 300 s left, below the 360 s margin
        await manager.ensure_fresh()
        self.assertEqual(len(session.token_posts), 1)

    async def test_token_obtained_after_its_lifetime_is_never_fresh(self) -> None:
        manager = self.fx.manager(self.fx.session())
        manager._options.token = jwt.encode({"exp": int(self.clock.now) + 1000}, JWT_KEY, algorithm="HS256")
        manager._obtained_at = self.clock.now + 2010  # half lifetime < 0 (no iat)
        self.assertFalse(manager._fresh_enough())


class SecondReviewTest(ManagerTestCase):
    """Code, security and verifier review of PR 4, second round."""

    def login_at(self, offset: float, generation: int, access: str | None, login_id: str, refresh: str = "FAKE-refresh-login"):
        """Clock hook: write a login once the fake clock reaches start + ``offset``; records when."""
        start, written = self.clock.now, {}

        def hook(clock):
            if not written and clock.now >= start + offset:
                write_auth_file(self.fx.path, generation, refresh, access, login_id=login_id)
                written["t"] = clock.now

        self.clock.hooks.append(hook)
        return written

    async def test_login_during_a_long_k3_wait_is_adopted_within_10_s(self) -> None:
        # MINOR-1: Retry-After 3000 s; the login 95 s later must not wait for the rest of it.
        self.fx.seed(generation=2, remaining=-1)
        def busy(session, kwargs):
            raise http_error(429, {"Retry-After": "3000"})

        session = self.fx.session(token_handler=busy)
        manager = self.fx.manager(session)
        login_access = make_jwt(self.clock.now + 95, ASSUMED_LIFETIME)
        written = self.login_at(95, 3, login_access, "beef")
        await manager.ensure_fresh()
        self.assertEqual((manager.state, manager._options.token), (READY, login_access))
        self.assertLessEqual(self.clock.now - written["t"], 10)
        self.assertEqual(len(session.token_posts), 1)
        self.assertEqual(self.fx.states, [DISCONNECTED, READY])

    async def test_new_login_opens_a_new_k3_window(self) -> None:
        # Security I3: the adopted login without an access token is refreshed at once, not after not_before.
        self.fx.seed(generation=2, remaining=-1)
        answers = [http_error(429, {"Retry-After": "3000"})]

        def handler(session, kwargs):
            if answers:
                raise answers.pop(0)
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        written = self.login_at(95, 3, None, "beef")
        await manager.ensure_fresh()
        self.assertEqual([b["refresh_token"] for b in session.token_posts], ["FAKE-refresh-seed", "FAKE-refresh-login"])
        self.assertLessEqual(session.post_times[1] - written["t"], 10)
        self.assertEqual(manager.state, READY)

    async def test_adopted_login_that_fails_again_is_a_new_auth_required_event(self) -> None:
        # MINOR-2: auth_required -> starting (login adopted) -> auth_required with a second event.
        self.fx.seed(generation=2, remaining=-1)
        session = self.fx.session(token_handler=reject(401))
        manager = self.fx.manager(session)
        self.login_at(25, 3, None, "cafe")
        self.clock.stop_at = self.clock.now + 60
        caller = await self.park(manager.ensure_fresh())
        try:
            self.assertEqual(self.fx.states, [AUTH_REQUIRED, STARTING, AUTH_REQUIRED])
            self.assertEqual(self.fx.events, ["AUTH_REQUIRED", "AUTH_REQUIRED"])
            self.assertEqual([b["refresh_token"] for b in session.token_posts], ["FAKE-refresh-seed", "FAKE-refresh-login"])
        finally:
            await self.stop(manager, caller)

    async def test_backoff_starts_over_after_a_login(self) -> None:
        # MINOR-8: K4 backoff 30, 60, 120; a login during the 240 s wait resets it to 30 s.
        self.fx.seed(generation=2, remaining=-1)
        failures = [5]

        def handler(session, kwargs):
            if failures[0]:
                failures[0] -= 1
                raise connector_error()
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        self.login_at(215, 3, None, "f00d")
        await manager.ensure_fresh()
        self.assertEqual(session.gaps(), [30, 60, 120, 10, 30])
        self.assertEqual(manager.state, READY)

    async def test_clock_ahead_refreshes_at_most_once_per_guard_interval(self) -> None:
        # MINOR-3: the local clock runs 2 h ahead, so every new token looks expired on arrival.
        self.fx.seed(remaining=None)

        def server_time(session, kwargs):
            access = make_jwt(session.clock.now - 7200, ASSUMED_LIFETIME)
            session.issued.append((access, f"FAKE-refresh-issued-{len(session.issued) + 1}"))
            return FakeResponse(200, {"access_token": access, "refresh_token": session.issued[-1][1]})

        session = self.fx.session(token_handler=server_time)
        manager = self.fx.manager(session)
        for _ in range(60):  # one poll every 10 s for 10 min
            await manager.ensure_fresh()
            await self.clock.sleep(10)
        self.assertLessEqual(len(session.token_posts), 11)
        self.assertGreaterEqual(min(session.gaps()), tm_module.CRASH_LOOP_GUARD)
        warnings = [m for m in self.log.messages(logging.WARNING) if "clock ahead" in m]
        self.assertEqual(len(warnings), 1)

    async def test_nothing_starts_after_aclose(self) -> None:
        # MINOR-5
        manager = self.fx.manager(self.fx.session())
        await manager.aclose()
        with self.assertRaises(TokenManagerClosedError):
            manager._start()
        with self.assertRaises(TokenManagerClosedError):
            await manager._wait_for_login()
        self.assertEqual((self.fx.states, self.fx.events), ([], []))
        self.assertIsNone(manager._task)

    async def test_rejected_token_is_not_lost_when_joining_a_foreign_task(self) -> None:
        # MINOR-6: the joined task adopts a login token without a POST; that very token was rejected.
        self.fx.seed(generation=1, remaining=3000)
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        fd = os.open(self.fx.store.lock_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            foreign = asyncio.create_task(manager.refresh_locked())
            while manager.phase is not Phase.ACQUIRING:
                await asyncio.sleep(0.01)
            login_access = make_jwt(self.clock.now, ASSUMED_LIFETIME)
            write_auth_file(self.fx.path, 2, "FAKE-refresh-login", login_access, login_id="d00d")
            rejecting = asyncio.create_task(manager.refresh_locked(rejected_token=login_access))
            await asyncio.sleep(0.05)
        finally:
            os.close(fd)
        await asyncio.gather(foreign, rejecting)
        self.assertEqual([b["refresh_token"] for b in session.token_posts], ["FAKE-refresh-login"])
        self.assertEqual(manager._options.refresh_token, "FAKE-refresh-issued-1")

    async def test_only_a_different_login_id_is_adopted(self) -> None:
        # Verifier MINOR-1: same generation and refresh token, only login_id differs.
        first = make_jwt(self.clock.now, ASSUMED_LIFETIME)
        write_auth_file(self.fx.path, 2, "FAKE-refresh-same", first, login_id="aa")
        session = self.fx.session()
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        second = make_jwt(self.clock.now - 1, ASSUMED_LIFETIME)
        write_auth_file(self.fx.path, 2, "FAKE-refresh-same", second, login_id="bb")
        await manager.ensure_fresh()
        self.assertEqual(manager._options.token, second)
        self.assertEqual(session.token_posts, [])

    async def test_refresh_posts_from_the_file_count_towards_the_k3_limit_after_a_restart(self) -> None:
        # Verifier MINOR-2: two POSTs before the restart plus one now reach the limit of three.
        start = self.clock.now
        write_auth_file(self.fx.path, 2, "FAKE-refresh-seed", None, refresh_posts=[start - 100, start - 50])
        answers = [http_error(429, {"Retry-After": "0"})]

        def handler(session, kwargs):
            if answers:
                raise answers.pop(0)
            return session.issue(kwargs)

        session = self.fx.session(token_handler=handler)
        manager = self.fx.manager(session)
        await manager.ensure_fresh()
        self.assertEqual(session.post_times, [start, start - 100 + 3600])  # not start + 60

    async def test_k3_wait_beyond_one_hour_is_implausible_and_ignored(self) -> None:
        # Verifier MINOR-3: not_before more than 1 h ahead is ignored, within 1 h it is honoured.
        for ahead, expected in ((3601, 0), (1800, 1800)):
            with self.subTest(ahead=ahead):
                self.setUpFixture()
                start = self.clock.now
                write_auth_file(self.fx.path, 2, "FAKE-refresh-seed", None, not_before=start + ahead, refresh_posts=[])
                session = self.fx.session()
                await self.fx.manager(session).ensure_fresh()
                self.assertEqual(session.post_times, [start + expected])

    async def test_block_applies_only_to_its_own_generation(self) -> None:
        # Verifier MINOR-3: blocked_generation is compared with the file's generation.
        for blocked_generation, posts in ((2, 1), (3, 0)):
            with self.subTest(blocked_generation=blocked_generation):
                self.setUpFixture()
                write_auth_file(self.fx.path, 3, "FAKE-refresh-seed", None, refresh_blocked="K2",
                                blocked_generation=blocked_generation)
                session = self.fx.session()
                manager = self.fx.manager(session)
                self.clock.stop_at = self.clock.now + 5
                caller = await self.park(manager.ensure_fresh())
                self.assertEqual(len(session.token_posts), posts)
                self.assertEqual(manager.state, READY if posts else AUTH_REQUIRED)
                await self.stop(manager, caller)

    def setUpFixture(self) -> None:
        self.fx = ManagerFixture(Path(tempfile.mkdtemp(dir=self._tmp.name)))
        self.clock = self.fx.clock


class ClaimsTest(unittest.TestCase):
    """M-2: implausible exp/iat never crash and never look fresh."""

    @staticmethod
    def token(**claims) -> str:
        return jwt.encode(claims, JWT_KEY, algorithm="HS256")

    def test_exp_and_iat_are_validated(self) -> None:
        self.assertEqual(token_times(self.token(exp=200, iat=100)), (200.0, 100.0))
        for exp in (True, "200", float("nan"), float("inf"), 10**400, 0, -5, None):
            with self.subTest(exp=exp):
                self.assertIsNone(token_times(self.token(exp=exp)))
        for iat in (200, 300, True, "100", float("nan")):
            with self.subTest(iat=iat):
                self.assertEqual(token_times(self.token(exp=200, iat=iat)), (200.0, None))
        for token in (None, "", 5, "FAKE-not-a-jwt"):
            with self.subTest(token=token):
                self.assertIsNone(token_times(token))

    def test_persistable_exp_is_a_positive_integer(self) -> None:
        self.assertIsNone(persistable_exp(self.token(exp=0.5)))
        self.assertEqual(persistable_exp(self.token(exp=200.7)), 200)
        self.assertIsNone(persistable_exp(self.token(exp=10**400)))

    def test_exp_more_than_30_days_ahead_is_unreadable(self) -> None:
        # Security 4: a token that would look fresh for months is treated like an unreadable one.
        now = 1_600_000_000
        self.assertEqual(token_times(self.token(exp=now + MAX_EXP_AHEAD), now), (now + MAX_EXP_AHEAD, None))
        self.assertIsNone(token_times(self.token(exp=now + MAX_EXP_AHEAD + 1), now))
        self.assertIsNone(persistable_exp(self.token(exp=now + MAX_EXP_AHEAD + 1), now))
        self.assertIsNone(token_times(self.token(exp=int(time.time()) + 40 * 86400)))  # wall clock by default


if __name__ == "__main__":
    unittest.main()
