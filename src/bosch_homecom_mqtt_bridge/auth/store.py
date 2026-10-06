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

Files: ``auth.lock`` and ``auth.json`` are opened with ``O_NOFOLLOW`` and must be regular files.
Every ``OSError`` surfaces as ``AuthStorageError`` naming only the path and the errno name.

Schema: ``{refresh_token, brand, generation, updated_at, last_refresh_at, access_token, exp,
login_id, refresh_blocked, blocked_generation, not_before, refresh_posts}``.
Access token and ``exp`` are persisted (decision D7, ADR 0001). Every field after ``last_refresh_at``
is optional, so older files stay readable. ``last_refresh_at`` and ``login_id`` are carried over
unless the update sets them; only the service refresh sets ``last_refresh_at``, only ``login`` sets
a new random ``login_id`` (file identity, ADR 0001).

The last four fields are the service's refresh bookkeeping (``RefreshMarks``), so it survives a
restart: ``refresh_blocked`` names the table K class that requires a new login and applies only while
``blocked_generation`` equals ``generation``; ``not_before`` and ``refresh_posts`` (epoch seconds)
carry the K3 limit. A token write clears them, except ``refresh_posts`` it passes on itself. A marks
write without ``blocked`` keeps a block that already applies to the current generation: only a login
lifts it.

Value ranges: times and ``exp`` lie in ``[0, MAX_EPOCH]``, ``refresh_blocked`` is ``K0`` to ``K10``
and ``login_id`` is lower-case hex; anything else makes the file invalid, never a crash.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import glob
import json
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

LOCK_FILE_NAME = "auth.lock"
DEFAULT_LOCK_TIMEOUT = 30.0  # longer than the 15 s request timeout of a refresh POST
DEFAULT_POLL_INTERVAL = 0.1
MAX_AUTH_FILE_SIZE = 64 * 1024  # far above any valid file (tokens are a few KiB at most)
MAX_REFRESH_POSTS = 16
MAX_EPOCH = 10**11  # year ~5138; far beyond any real time, small enough for float and datetime
BLOCK_CLASSES = frozenset(f"K{number}" for number in range(11))  # table K class names
_LOGIN_ID = re.compile(r"[0-9a-f]{1,64}")

_LOGGER = logging.getLogger(__name__)


class AuthStoreError(Exception):
    """Base class for token store errors."""


class LockTimeoutError(AuthStoreError):
    """The lock could not be acquired in time; nothing was read or written."""


class InvalidAuthFileError(AuthStoreError):
    """``auth.json`` exists but is not a regular file, too large, unparsable or incomplete."""


class AuthStorageError(AuthStoreError):
    """A file system operation on ``auth.json``, its temp file or ``auth.lock`` failed."""


def _storage_error(path: Path, error: OSError) -> AuthStorageError:
    """Message with path and errno name only; ``strerror`` and arguments are left out."""
    name = errno.errorcode.get(error.errno, "unknown error") if error.errno else "unknown error"
    return AuthStorageError(f"{path}: {name}")


@dataclass(frozen=True)
class AuthState:
    """Content of ``auth.json``."""

    refresh_token: str = field(repr=False)
    brand: str
    generation: int
    updated_at: str
    last_refresh_at: str | None
    access_token: str | None = field(default=None, repr=False)
    exp: int | None = None
    login_id: str | None = None
    refresh_blocked: str | None = None
    blocked_generation: int | None = None
    not_before: float | None = None
    refresh_posts: tuple[float, ...] = ()


@dataclass(frozen=True)
class AuthUpdate:
    """New token data to persist; the store assigns generation and ``updated_at``.

    ``last_refresh_at`` is set only by the service refresh, ``login_id`` only by ``login``;
    ``None`` keeps the stored value.
    """

    refresh_token: str = field(repr=False)
    brand: str
    access_token: str | None = field(default=None, repr=False)
    exp: int | None = None
    last_refresh_at: str | None = None
    login_id: str | None = None
    refresh_posts: tuple[float, ...] = ()


@dataclass(frozen=True)
class RefreshMarks:
    """Bookkeeping write without new tokens: generation, tokens and ``updated_at`` stay as they are.

    ``blocked`` (a table K class name) blocks the current generation.
    """

    blocked: str | None = None
    not_before: float | None = None
    posts: tuple[float, ...] = ()


def _valid_access(access_token: object, exp: object) -> bool:
    """Both absent, or a non-empty access token with an optional integer ``exp`` in ``(0, MAX_EPOCH]``."""
    if access_token is None:
        return exp is None
    return (
        isinstance(access_token, str)
        and bool(access_token)
        and (exp is None or (isinstance(exp, int) and not isinstance(exp, bool) and 0 < exp <= MAX_EPOCH))
    )


def _valid_login_id(value: object) -> bool:
    return value is None or (isinstance(value, str) and _LOGIN_ID.fullmatch(value) is not None)


def _valid_time(value: object) -> bool:
    # Range check only: nan and inf fail it, and no huge integer is ever converted to float.
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0 <= value <= MAX_EPOCH


def _valid_marks(blocked: object, blocked_generation: object, not_before: object, posts: object) -> bool:
    """``blocked`` (``K0`` to ``K10``) and ``blocked_generation`` together or neither; valid times."""
    if (blocked is None) != (blocked_generation is None) or not (blocked is None or blocked in BLOCK_CLASSES):
        return False
    if blocked_generation is not None and (
        isinstance(blocked_generation, bool) or not isinstance(blocked_generation, int) or blocked_generation < 1
    ):
        return False
    if not_before is not None and not _valid_time(not_before):
        return False
    return (
        isinstance(posts, (list, tuple))
        and len(posts) <= MAX_REFRESH_POSTS
        and all(_valid_time(post) for post in posts)
    )


UpdateFn = Callable[[AuthState | None], Awaitable[AuthUpdate | RefreshMarks | None]]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuthStore:
    """Use one instance per ``auth.json`` and process; the in-process lock lives on the instance.

    The lock is not reentrant: ``fn`` passed to ``locked_update`` must not call methods of the
    same store, it would wait for itself until ``LockTimeoutError``.
    """

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
        caller adopts instead). The new generation is the file's generation plus one. ``fn`` may
        instead return ``RefreshMarks``: then only the bookkeeping changes, the generation stays.
        Returns the state now in the file. If ``fn`` raises, the file stays untouched.

        Cancellation while ``fn`` runs discards whatever ``fn`` obtained: if ``fn`` redeems a
        single-use token, the result is lost. Callers that must not lose it shield the call
        (``asyncio.shield``) or keep cancellation away from it.

        Raises ``LockTimeoutError``, ``InvalidAuthFileError`` or ``AuthStorageError``; with
        ``AuthStorageError`` from the write, ``fn`` has already run. ``ValueError`` means ``fn``
        returned data that fails validation; nothing was written.
        """
        async with self._locked():
            current = self._read()
            update = await fn(current)
            if update is None:
                return current
            if isinstance(update, RefreshMarks):
                if current is None:
                    raise ValueError("refusing to write refresh marks without an auth file")
                blocked = update.blocked
                if blocked is None and current.blocked_generation == current.generation:
                    blocked = current.refresh_blocked  # an existing block of this generation stays
                new_state = replace(
                    current,
                    refresh_blocked=blocked,
                    blocked_generation=current.generation if blocked is not None else None,
                    not_before=update.not_before,
                    refresh_posts=tuple(update.posts),
                )
            else:
                if not isinstance(update.refresh_token, str) or not update.refresh_token:
                    raise ValueError("refusing to write an empty refresh token")
                if not _valid_access(update.access_token, update.exp):
                    raise ValueError("refusing to write an empty access token or an invalid exp")
                new_state = AuthState(
                    refresh_token=update.refresh_token,
                    brand=update.brand,
                    generation=(current.generation if current else 0) + 1,
                    updated_at=_now(),
                    last_refresh_at=update.last_refresh_at or (current.last_refresh_at if current else None),
                    access_token=update.access_token,
                    exp=update.exp,
                    login_id=update.login_id or (current.login_id if current else None),
                    refresh_posts=tuple(update.refresh_posts),
                )
            if not _valid_login_id(new_state.login_id) or not _valid_marks(
                new_state.refresh_blocked, new_state.blocked_generation, new_state.not_before, new_state.refresh_posts
            ):
                raise ValueError("refusing to write invalid refresh marks or login id")
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
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            self._local_lock.release()

    async def _acquire_flock(self, deadline: float) -> int:
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as error:
            raise _storage_error(self.lock_path, error) from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise AuthStorageError(f"{self.lock_path}: not a regular file")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError as error:
                    if error.errno not in (errno.EAGAIN, errno.EACCES):
                        raise _storage_error(self.lock_path, error) from None
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"timed out waiting for {self.lock_path}")
                await asyncio.sleep(self.poll_interval)
        except BaseException:
            # Covers timeout and cancellation: the descriptor never holds the lock afterwards.
            os.close(fd)
            raise

    def _read(self) -> AuthState | None:
        invalid = InvalidAuthFileError(
            f"{self.path} is not a valid auth file; back it up and remove it, then run login again"
        )
        try:
            # O_NONBLOCK: opening a FIFO planted at the path must not hang.
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _storage_error(self.path, error) from None
        chunks = []
        size = 0
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise invalid
            while chunk := os.read(fd, 65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_AUTH_FILE_SIZE:
                    raise invalid
        except OSError as error:
            raise _storage_error(self.path, error) from None
        finally:
            os.close(fd)
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError and JSONDecodeError
            raise invalid from None
        if not isinstance(data, dict):
            raise invalid
        refresh_token = data.get("refresh_token")
        brand = data.get("brand")
        generation = data.get("generation")
        updated_at = data.get("updated_at")
        last_refresh_at = data.get("last_refresh_at")
        access_token = data.get("access_token")
        exp = data.get("exp")
        login_id = data.get("login_id")
        blocked = data.get("refresh_blocked")
        blocked_generation = data.get("blocked_generation")
        not_before = data.get("not_before")
        posts = data.get("refresh_posts") or []  # null counts as missing
        try:
            valid = (
                isinstance(refresh_token, str)
                and bool(refresh_token)
                and isinstance(brand, str)
                and not isinstance(generation, bool)
                and isinstance(generation, int)
                and generation >= 1
                and isinstance(updated_at, str)
                and (last_refresh_at is None or isinstance(last_refresh_at, str))
                and _valid_access(access_token, exp)
                and _valid_login_id(login_id)
                and _valid_marks(blocked, blocked_generation, not_before, posts)
            )
        except (OverflowError, TypeError):  # defence in depth: a value must never crash the reader
            valid = False
        if not valid:
            raise invalid
        return AuthState(
            refresh_token, brand, generation, updated_at, last_refresh_at, access_token, exp,
            login_id, blocked, blocked_generation, not_before, tuple(posts),
        )

    def _remove_stale_temp_files(self) -> None:
        """Delete temp files of crashed writers; called under the lock.

        Only files older than ``lock_timeout`` are removed, so a writer that does not use this
        lock (none should exist) is not disturbed mid-write.
        """
        cutoff = time.time() - self.lock_timeout
        for candidate in self.path.parent.glob(f".{glob.escape(self.path.name)}.*.tmp"):
            with contextlib.suppress(OSError):
                info = candidate.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    candidate.unlink()

    def _write(self, state: AuthState) -> None:
        """Write atomically: unique temp file (mode 600), fsync, rename, fsync of the directory.

        Durability is best effort: once ``os.replace`` succeeded the new file is in place, and a
        failing ``fsync`` of the directory is only logged.
        """
        payload = json.dumps(
            {
                "refresh_token": state.refresh_token,
                "brand": state.brand,
                "generation": state.generation,
                "updated_at": state.updated_at,
                "last_refresh_at": state.last_refresh_at,
                "access_token": state.access_token,
                "exp": state.exp,
                "login_id": state.login_id,
                "refresh_blocked": state.refresh_blocked,
                "blocked_generation": state.blocked_generation,
                "not_before": state.not_before,
                "refresh_posts": list(state.refresh_posts),
            }
        ).encode("utf-8")
        self._remove_stale_temp_files()
        # mkstemp creates the file exclusively with mode 0600 under a fresh name, so a temp file
        # left behind by a crashed writer never blocks the next write.
        try:
            fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        except OSError as error:
            raise _storage_error(self.path.parent, error) from None
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
        except BaseException as error:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            if isinstance(error, OSError):
                raise _storage_error(self.path, error) from None
            raise
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as error:
            _LOGGER.warning(
                "auth.json was replaced, but syncing the directory failed (%s); durability is best effort",
                _storage_error(self.path.parent, error),
            )
