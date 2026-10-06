import asyncio
import errno
import fcntl
import gc
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from homecom_alt.const import DEFAULT_TIMEOUT

from bosch_homecom_mqtt_bridge.auth import store as store_module
from bosch_homecom_mqtt_bridge.auth.store import (
    DEFAULT_LOCK_TIMEOUT,
    MAX_AUTH_FILE_SIZE,
    AuthState,
    AuthStorageError,
    AuthStore,
    AuthUpdate,
    InvalidAuthFileError,
    LockTimeoutError,
)

ROOT = Path(__file__).resolve().parents[1]
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

# Holds an exclusive flock on argv[1] until a line arrives on stdin.
HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.readline()
"""

# Exits 0 if it gets the flock on argv[1] immediately, 1 otherwise.
TRY_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
"""

# One side of the race: "login" always writes, "service" writes only on the generation it knew
# before taking the lock and otherwise adopts the file (discarding its own result).
RACE_WORKER = """
import asyncio, json, sys
from bosch_homecom_mqtt_bridge.auth.store import AuthStore, AuthUpdate

path, role, runs = sys.argv[1], sys.argv[2], int(sys.argv[3])

async def main():
    store = AuthStore(path, poll_interval=0.001)
    log = []
    for i in range(runs):
        known = await store.read_locked()
        known_generation = known.generation if known else 0
        record = {}

        async def fn(current):
            base = current.generation if current else 0
            record["base"] = base
            await asyncio.sleep(0.001)
            if role == "service" and base != known_generation:
                return None
            record["token"] = f"FAKE-{role}-{base}-{i}"
            return AuthUpdate(record["token"], "bosch")

        state = await store.locked_update(fn)
        if "token" in record:
            record["generation"] = state.generation
            record["known"] = known_generation
        log.append(record)
    print(json.dumps(log))

asyncio.run(main())
"""


def start_holder(lock_path: Path) -> subprocess.Popen:
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    if holder.stdout.readline().strip() != "locked":
        holder.kill()
        raise RuntimeError("lock holder did not start")
    return holder


def release_holder(holder: subprocess.Popen) -> None:
    holder.communicate("\n", timeout=10)


def lock_is_free(lock_path: Path) -> bool:
    """Ask another process, so that this process' own flock state cannot fool the check."""
    result = subprocess.run([sys.executable, "-c", TRY_LOCK, str(lock_path)], timeout=10)
    return result.returncode == 0


def open_fds() -> int:
    gc.collect()  # close descriptors of unreachable objects (event loops, sockets) before counting
    return len(os.listdir("/proc/self/fd"))


def update(token: str, brand: str = "bosch"):
    async def fn(_current: AuthState | None) -> AuthUpdate:
        return AuthUpdate(token, brand)

    return fn


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "auth.json"
        self.store = AuthStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_raw(self, data: dict) -> bytes:
        raw = json.dumps(data).encode()
        self.path.write_bytes(raw)
        return raw


class AtomicWriteTest(StoreTestCase):
    def test_first_write_has_mode_600_and_schema(self) -> None:
        state = asyncio.run(self.store.locked_update(update("FAKE-refresh-1")))
        self.assertEqual(state.generation, 1)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        data = json.loads(self.path.read_text())
        self.assertEqual(
            set(data), {"refresh_token", "brand", "generation", "updated_at", "last_refresh_at"}
        )
        self.assertEqual(data["refresh_token"], "FAKE-refresh-1")
        self.assertEqual(data["generation"], 1)
        self.assertIsNone(data["last_refresh_at"])
        self.assertEqual(stat.S_IMODE((self.dir / "auth.lock").stat().st_mode), 0o600)

    def test_generation_increments_and_last_refresh_is_carried_over(self) -> None:
        self.write_raw(
            {
                "refresh_token": "FAKE-old",
                "brand": "bosch",
                "generation": 7,
                "updated_at": "2026-10-04T10:00:00+00:00",
                "last_refresh_at": "2026-10-04T09:00:00+00:00",
            }
        )
        os.chmod(self.path, 0o644)
        state = asyncio.run(self.store.locked_update(update("FAKE-new", "buderus")))
        self.assertEqual(state.generation, 8)
        self.assertEqual(state.brand, "buderus")
        self.assertEqual(state.last_refresh_at, "2026-10-04T09:00:00+00:00")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(asyncio.run(self.store.read_locked()), state)

    def test_abort_before_rename_keeps_old_file(self) -> None:
        asyncio.run(self.store.locked_update(update("FAKE-old")))
        before = self.path.read_bytes()
        error = OSError(errno.ENOSPC, "FAKE-strerror")
        with mock.patch.object(store_module.os, "replace", side_effect=error):
            with self.assertRaises(AuthStorageError) as ctx:
                asyncio.run(self.store.locked_update(update("FAKE-new")))
        self.assertEqual(str(ctx.exception), f"{self.path}: ENOSPC")
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["auth.json", "auth.lock"])

    def test_failed_directory_fsync_after_replace_is_logged_and_swallowed(self) -> None:
        real_fsync = os.fsync
        calls = []

        def fsync(fd: int) -> None:
            calls.append(fd)
            if len(calls) == 2:  # 1: temp file, 2: directory
                raise OSError(errno.EIO, "FAKE-strerror")
            real_fsync(fd)

        with mock.patch.object(store_module.os, "fsync", side_effect=fsync):
            with self.assertLogs(store_module.__name__, "WARNING") as logs:
                state = asyncio.run(self.store.locked_update(update("FAKE-new")))
        self.assertEqual(state.generation, 1)
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "FAKE-new")
        self.assertIn("EIO", logs.output[0])
        self.assertNotIn("FAKE-", logs.output[0])

    def test_stale_temp_files_are_removed_under_lock(self) -> None:
        old = self.dir / ".auth.json.crashed.tmp"
        fresh = self.dir / ".auth.json.recent.tmp"
        unrelated = self.dir / ".other.json.crashed.tmp"
        for path in (old, fresh, unrelated):
            path.write_text("partial")
        past = time.time() - DEFAULT_LOCK_TIMEOUT - 60
        os.utime(old, (past, past))
        os.utime(unrelated, (past, past))
        real_remove = AuthStore._remove_stale_temp_files
        held_during_cleanup = []

        def remove(store: AuthStore) -> None:
            held_during_cleanup.append(not lock_is_free(store.lock_path))
            real_remove(store)

        with mock.patch.object(AuthStore, "_remove_stale_temp_files", remove):
            asyncio.run(self.store.locked_update(update("FAKE-new")))
        self.assertEqual(held_during_cleanup, [True])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())

    def test_stale_temp_file_cleanup_escapes_glob_characters(self) -> None:
        # Unescaped, ".auth[1].json.*.tmp" would match ".auth1.json..." and miss its own temp file.
        store = AuthStore(self.dir / "auth[1].json")
        own = self.dir / ".auth[1].json.crashed.tmp"
        other = self.dir / ".auth1.json.crashed.tmp"
        past = time.time() - DEFAULT_LOCK_TIMEOUT - 60
        for path in (own, other):
            path.write_text("partial")
            os.utime(path, (past, past))
        asyncio.run(store.locked_update(update("FAKE-new")))
        self.assertFalse(own.exists())
        self.assertTrue(other.exists())

    def test_leftover_temp_file_does_not_block_next_write(self) -> None:
        stale = self.dir / ".auth.json.stale.tmp"
        stale.write_text("partial")
        os.chmod(stale, 0o400)
        state = asyncio.run(self.store.locked_update(update("FAKE-after-crash")))
        self.assertEqual(state.generation, 1)
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "FAKE-after-crash")

    def test_empty_or_missing_token_is_never_written(self) -> None:
        asyncio.run(self.store.locked_update(update("FAKE-old")))
        before = self.path.read_bytes()
        for token in ("", None):
            with self.subTest(token=token), self.assertRaises(ValueError):
                asyncio.run(self.store.locked_update(update(token)))  # type: ignore[arg-type]
        self.assertEqual(self.path.read_bytes(), before)

    def test_fn_returning_none_writes_nothing(self) -> None:
        first = asyncio.run(self.store.locked_update(update("FAKE-old")))
        before = self.path.read_bytes()

        async def adopt(_current: AuthState | None) -> None:
            return None

        self.assertEqual(asyncio.run(self.store.locked_update(adopt)), first)
        self.assertEqual(self.path.read_bytes(), before)

    def test_fn_raising_leaves_file_untouched(self) -> None:
        asyncio.run(self.store.locked_update(update("FAKE-old")))
        before = self.path.read_bytes()

        async def failing(_current: AuthState | None) -> AuthUpdate:
            raise RuntimeError("exchange failed")

        with self.assertRaises(RuntimeError):
            asyncio.run(self.store.locked_update(failing))
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_caller_adopts_newer_login(self) -> None:
        asyncio.run(self.store.locked_update(update("FAKE-service-base")))
        known_generation = 1
        asyncio.run(self.store.locked_update(update("FAKE-login-new")))

        async def service(current: AuthState | None) -> AuthUpdate | None:
            assert current is not None
            if current.generation != known_generation:
                return None
            return AuthUpdate("FAKE-service-result", "bosch")

        state = asyncio.run(self.store.locked_update(service))
        self.assertEqual((state.generation, state.refresh_token), (2, "FAKE-login-new"))


class ReadTest(StoreTestCase):
    def test_missing_file(self) -> None:
        self.assertIsNone(asyncio.run(self.store.read_locked()))

    def test_invalid_files_are_rejected_without_echoing_content(self) -> None:
        valid = {"refresh_token": "FAKE-x", "brand": "bosch", "generation": 1, "updated_at": "t"}
        cases = [
            "not json FAKE-secret-content",
            json.dumps(["FAKE-secret-content"]),
            json.dumps({**valid, "refresh_token": ""}),
            json.dumps({**valid, "generation": 0}),
            json.dumps({**valid, "generation": True}),
            json.dumps({**valid, "brand": None}),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises(InvalidAuthFileError) as ctx:
                    asyncio.run(self.store.read_locked())
                self.assertNotIn("FAKE-", str(ctx.exception))
                self.assertIn("login", str(ctx.exception))

    def test_invalid_utf8_is_an_invalid_file(self) -> None:
        self.path.write_bytes(b'{"refresh_token": "\xff\xfe"}')
        with self.assertRaises(InvalidAuthFileError):
            asyncio.run(self.store.read_locked())

    def test_oversized_file_is_an_invalid_file(self) -> None:
        valid = {"refresh_token": "FAKE-x", "brand": "bosch", "generation": 1, "updated_at": "t"}
        raw = json.dumps(valid).encode()
        self.path.write_bytes(raw[:-1] + b" " * (MAX_AUTH_FILE_SIZE - len(raw)) + b"}")
        self.assertEqual(self.path.stat().st_size, MAX_AUTH_FILE_SIZE)
        self.assertEqual(asyncio.run(self.store.read_locked()).generation, 1)
        self.path.write_bytes(raw[:-1] + b" " * (MAX_AUTH_FILE_SIZE + 1 - len(raw)) + b"}")
        with self.assertRaises(InvalidAuthFileError):
            asyncio.run(self.store.read_locked())

    def test_non_regular_auth_file_is_rejected(self) -> None:
        self.path.mkdir()
        with self.assertRaises(InvalidAuthFileError):
            asyncio.run(self.store.read_locked())

    def test_symlinked_auth_file_is_not_followed(self) -> None:
        target = self.dir / "elsewhere.json"
        self.write_raw({"refresh_token": "FAKE-x", "brand": "bosch", "generation": 1, "updated_at": "t"})
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(AuthStorageError) as ctx:
            asyncio.run(self.store.read_locked())
        self.assertEqual(str(ctx.exception), f"{self.path}: ELOOP")

    def test_symlinked_or_special_lock_file_is_rejected(self) -> None:
        lock_path = self.dir / "auth.lock"
        lock_path.symlink_to(self.dir / "lock-target")
        with self.assertRaises(AuthStorageError) as ctx:
            asyncio.run(self.store.read_locked())
        self.assertIn("ELOOP", str(ctx.exception))
        self.assertFalse((self.dir / "lock-target").exists())
        lock_path.unlink()
        os.mkfifo(lock_path)
        with self.assertRaises(AuthStorageError) as ctx:
            asyncio.run(self.store.read_locked())
        self.assertIn("not a regular file", str(ctx.exception))


class InProcessLockTest(StoreTestCase):
    def test_concurrent_updates_are_serialised(self) -> None:
        other = AuthStore(self.path, poll_interval=0.001)

        def slow(token: str):
            async def fn(_current: AuthState | None) -> AuthUpdate:
                await asyncio.sleep(0.05)
                return AuthUpdate(token, "bosch")

            return fn

        async def main() -> list[int]:
            stores = [self.store, self.store, other]
            results = await asyncio.gather(*(s.locked_update(slow(f"FAKE-{n}")) for n, s in enumerate(stores)))
            return sorted(r.generation for r in results)

        self.assertEqual(asyncio.run(main()), [1, 2, 3])

    def test_cancelled_waiter_leaves_lock_free(self) -> None:
        fds_before = open_fds()
        holder = start_holder(self.dir / "auth.lock")
        try:

            async def main() -> None:
                task = asyncio.create_task(self.store.read_locked())
                await asyncio.sleep(0.3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(main())
        finally:
            release_holder(holder)
        # The descriptor opened while waiting for flock must be closed on cancellation.
        self.assertEqual(open_fds(), fds_before)
        fd = os.open(self.dir / "auth.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_cancel_during_fn_releases_lock_and_descriptor(self) -> None:
        asyncio.run(self.store.locked_update(update("FAKE-old")))
        before = self.path.read_bytes()
        fds_before = open_fds()
        lock_path = self.dir / "auth.lock"

        async def main() -> bool:
            entered = asyncio.Event()

            async def hanging(_current: AuthState | None) -> AuthUpdate:
                entered.set()
                await asyncio.sleep(3600)
                return AuthUpdate("FAKE-never", "bosch")

            task = asyncio.create_task(self.store.locked_update(hanging))
            await entered.wait()
            held = not await asyncio.to_thread(lock_is_free, lock_path)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return held

        self.assertTrue(asyncio.run(main()), "lock was not held while fn ran")
        self.assertEqual(open_fds(), fds_before)
        self.assertTrue(lock_is_free(lock_path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_failing_unlock_still_closes_descriptor(self) -> None:
        asyncio.run(self.store.locked_update(update("FAKE-old")))
        fds_before = open_fds()
        real_flock = fcntl.flock

        def flock(fd: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                raise OSError(errno.EIO, "FAKE-strerror")
            real_flock(fd, operation)

        with mock.patch.object(store_module.fcntl, "flock", side_effect=flock):
            with self.assertRaises(OSError):
                asyncio.run(self.store.read_locked())
        self.assertEqual(open_fds(), fds_before)
        # Closing the only descriptor released the flock as well.
        self.assertTrue(lock_is_free(self.dir / "auth.lock"))

    def test_in_process_lock_timeout(self) -> None:
        store = AuthStore(self.path, lock_timeout=0.2)
        fds_before = open_fds()

        async def main() -> None:
            entered = asyncio.Event()

            async def slow(_current: AuthState | None) -> AuthUpdate:
                entered.set()
                await asyncio.sleep(0.6)
                return AuthUpdate("FAKE-first", "bosch")

            first = asyncio.create_task(store.locked_update(slow))
            await entered.wait()
            with self.assertRaises(LockTimeoutError):
                await store.read_locked()
            self.assertEqual((await first).refresh_token, "FAKE-first")

        asyncio.run(main())
        self.assertEqual(open_fds(), fds_before)
        self.assertTrue(lock_is_free(self.dir / "auth.lock"))


class CrossProcessTest(StoreTestCase):
    def test_default_lock_timeout_exceeds_request_timeout(self) -> None:
        # Longer than the library's request timeout, so a refresh POST under the lock finishes first.
        self.assertGreater(DEFAULT_LOCK_TIMEOUT, DEFAULT_TIMEOUT.total)
        self.assertEqual(self.store.lock_timeout, DEFAULT_LOCK_TIMEOUT)

    def test_event_loop_keeps_running_while_lock_is_held(self) -> None:
        holder = start_holder(self.dir / "auth.lock")
        try:

            async def main() -> tuple[int, float]:
                ticks = 0

                async def ticker() -> None:
                    nonlocal ticks
                    while True:
                        await asyncio.sleep(0.01)
                        ticks += 1

                tick_task = asyncio.create_task(ticker())
                asyncio.get_running_loop().call_later(2.0, release_holder, holder)
                started = time.monotonic()
                await self.store.read_locked()
                elapsed = time.monotonic() - started
                tick_task.cancel()
                return ticks, elapsed

            ticks, elapsed = asyncio.run(main())
        finally:
            if holder.poll() is None:
                release_holder(holder)
        self.assertGreaterEqual(elapsed, 1.9)
        # 10 ms ticks over ~2 s: a blocked loop would yield almost none; the bound is loose for CI.
        self.assertGreater(ticks, 50)

    def test_lock_timeout_raises_and_leaves_no_lock_behind(self) -> None:
        # Scaled down from the plan (lock held 35 s, timeout 30 s) to keep the suite fast; the
        # mechanism does not depend on the absolute values.
        store = AuthStore(self.path, lock_timeout=1.0)
        fds_before = open_fds()
        holder = start_holder(self.dir / "auth.lock")
        try:
            started = time.monotonic()
            with self.assertRaises(LockTimeoutError):
                asyncio.run(store.locked_update(update("FAKE-never")))
            elapsed = time.monotonic() - started
        finally:
            release_holder(holder)
        # The descriptor opened while polling for flock is closed on timeout.
        self.assertEqual(open_fds(), fds_before)
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 3.0)
        self.assertFalse(self.path.exists())
        self.assertTrue(lock_is_free(self.dir / "auth.lock"))

    def test_race_between_two_processes(self) -> None:
        runs = 50  # per process, 100 runs in total
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", RACE_WORKER, str(self.path), role, str(runs)],
                stdout=subprocess.PIPE,
                env=SUBPROCESS_ENV,
                text=True,
            )
            for role in ("login", "service")
        ]
        logs = {}
        for role, worker in zip(("login", "service"), workers):
            out, _ = worker.communicate(timeout=120)
            self.assertEqual(worker.returncode, 0)
            logs[role] = json.loads(out)

        writes = [r for role in logs for r in logs[role] if "token" in r]
        generations = sorted(r["generation"] for r in writes)
        final = json.loads(self.path.read_text())
        # Strictly monotonic and nothing lost: every write got a unique generation, without gaps,
        # and the file holds the last one.
        self.assertEqual(generations, list(range(1, final["generation"] + 1)))
        self.assertEqual(final["refresh_token"], max(writes, key=lambda r: r["generation"])["token"])
        # Every write was based on the state directly before it.
        for record in writes:
            self.assertEqual(record["generation"], record["base"] + 1)
        # Login always writes; the service never writes on top of a base it did not know.
        self.assertEqual(sum("token" in r for r in logs["login"]), runs)
        for record in logs["service"]:
            if "token" in record:
                self.assertEqual(record["base"], record["known"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
