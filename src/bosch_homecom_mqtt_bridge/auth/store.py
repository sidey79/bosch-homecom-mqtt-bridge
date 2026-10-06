"""Persistent token store (``auth.json``) shared by the service and the ``login`` command.

Refresh tokens are single-use, so every write runs under an exclusive lock and re-reads the file
first: ``generation`` is only read and incremented while the lock is held.

Locking:
- The lock file is a separate ``auth.lock`` next to ``auth.json``. ``auth.json`` itself is
  replaced by ``rename`` on every write, so a lock on it would bind to a stale inode.
- Inside one process an ``asyncio.Lock`` serialises callers before they touch ``flock``.
- ``flock`` is only ever tried with ``LOCK_NB`` and polled with ``asyncio.sleep``, so the event
  loop never blocks. A thread doing a blocking ``flock`` is ruled out: if its waiter timed out,
  the thread would still get the lock later and nobody would release it.

``flock`` is reliable across containers only on local volumes, not on NFS or CIFS.

Schema: ``{refresh_token, brand, generation, updated_at, last_refresh_at}``. Access token and
``exp`` are not stored (open decision D7). ``last_refresh_at`` is carried over unchanged; only the
service refresh (token manager) will set it.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LOCK_FILE_NAME = "auth.lock"
DEFAULT_LOCK_TIMEOUT = 30.0  # longer than the 15 s request timeout of a refresh POST
DEFAULT_POLL_INTERVAL = 0.1


class AuthStoreError(Exception):
    """Base class for token store errors."""


class LockTimeoutError(AuthStoreError):
    """The lock could not be acquired in time; nothing was read or written."""


class InvalidAuthFileError(AuthStoreError):
    """``auth.json`` exists but cannot be parsed or misses required fields."""


@dataclass(frozen=True)
class AuthState:
    """Content of ``auth.json``."""

    refresh_token: str
    brand: str
    generation: int
    updated_at: str
    last_refresh_at: str | None


@dataclass(frozen=True)
class AuthUpdate:
    """New token data to persist; the store assigns generation and timestamps."""

    refresh_token: str
    brand: str


UpdateFn = Callable[[AuthState | None], Awaitable[AuthUpdate | None]]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuthStore:
    """Use one instance per ``auth.json`` and process; the in-process lock lives on the instance."""

    def __init__(
        self,
        path: Path,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.parent / LOCK_FILE_NAME
        self.lock_timeout = lock_timeout
        self.poll_interval = poll_interval
        self._local_lock = asyncio.Lock()

    async def read_locked(self) -> AuthState | None:
        """Read ``auth.json`` under the lock; ``None`` if it does not exist."""
        async with self._locked():
            return self._read()

    async def locked_update(self, fn: UpdateFn) -> AuthState | None:
        """Run ``fn(current)`` under the lock and persist its result.

        ``fn`` receives the state freshly read under the lock and returns the tokens to write,
        or ``None`` to write nothing (e.g. because the file holds a newer generation that the
        caller adopts instead). The new generation is the file's generation plus one. Returns
        the state now in the file. If ``fn`` raises, the file stays untouched.
        """
        async with self._locked():
            current = self._read()
            update = await fn(current)
            if update is None:
                return current
            if not isinstance(update.refresh_token, str) or not update.refresh_token:
                raise ValueError("refusing to write an empty refresh token")
            new_state = AuthState(
                refresh_token=update.refresh_token,
                brand=update.brand,
                generation=(current.generation if current else 0) + 1,
                updated_at=_now(),
                last_refresh_at=current.last_refresh_at if current else None,
            )
            self._write(new_state)
            return new_state

    @contextlib.asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        deadline = time.monotonic() + self.lock_timeout
        try:
            async with asyncio.timeout(self.lock_timeout):
                await self._local_lock.acquire()
        except TimeoutError:
            raise LockTimeoutError(f"timed out waiting for {self.lock_path}") from None
        try:
            fd = await self._acquire_flock(deadline)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            self._local_lock.release()

    async def _acquire_flock(self, deadline: float) -> int:
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError as error:
                    if error.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"timed out waiting for {self.lock_path}")
                await asyncio.sleep(self.poll_interval)
        except BaseException:
            # Covers timeout and cancellation: the descriptor never holds the lock afterwards.
            os.close(fd)
            raise

    def _read(self) -> AuthState | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        invalid = InvalidAuthFileError(f"{self.path} is not a valid auth file")
        try:
            data = json.loads(raw)
        except ValueError:
            raise invalid from None
        if not isinstance(data, dict):
            raise invalid
        refresh_token = data.get("refresh_token")
        brand = data.get("brand")
        generation = data.get("generation")
        updated_at = data.get("updated_at")
        last_refresh_at = data.get("last_refresh_at")
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(brand, str)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not isinstance(updated_at, str)
            or not (last_refresh_at is None or isinstance(last_refresh_at, str))
        ):
            raise invalid
        return AuthState(refresh_token, brand, generation, updated_at, last_refresh_at)

    def _write(self, state: AuthState) -> None:
        """Write atomically: unique temp file (mode 600), fsync, rename, fsync of the directory."""
        payload = json.dumps(
            {
                "refresh_token": state.refresh_token,
                "brand": state.brand,
                "generation": state.generation,
                "updated_at": state.updated_at,
                "last_refresh_at": state.last_refresh_at,
            }
        ).encode("utf-8")
        # mkstemp creates the file exclusively with mode 0600 under a fresh name, so a temp file
        # left behind by a crashed writer never blocks the next write.
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
