"""Token manager: refreshes only under the store lock, data requests never refresh (R-b, ADR 0001).

All instances share one ``ConnectionOptions``. Fetch instances (discovery, polls) use
``auth_provider=False``, so ``get_token()`` is a no-op there however many requests a poll makes.
Only the refresh instance (``auth_provider=True``) sends the refresh POST, and only inside
``AuthStore.locked_update``.

The refresh runs as one task per process. Callers wait for it through ``asyncio.shield``, so a
cancelled caller never aborts a POST. Phases of the task:

- ``acquiring`` (waiting for the lock or reading the file) and ``backoff`` (waiting between
  attempts, crash-loop guard, ``auth_required`` checks): nothing is in flight, ``aclose()``
  cancels them.
- ``sending`` and ``writing``: ``aclose()`` waits for them. The task enters ``sending`` in the
  first statement after the lock was acquired, without an ``await`` in between.

State changes (``disconnected``, ``auth_required``) happen inside the task. In ``auth_required``
no cloud request is sent; every 10 s the file is read under the lock, and a new login is adopted.
If the adopted login still needs a refresh, the state passes ``starting`` first, so a renewed
failure is a new ``auth_required`` transition with its own error event. Long K3/K4 waits are split
into 10-s steps with the same file check: a login ends the wait early and opens a new K3 window.

Clock running ahead: if a token just obtained is already due, a warning is logged once and no
further refresh starts for ``CRASH_LOOP_GUARD`` seconds, instead of one POST per poll.

File identity: the file is adopted whenever its generation, ``login_id`` or refresh token differ
from what this process last read or wrote. That covers a login, a deleted and recreated file whose
generation starts over at 1, and a restored backup.

Restart safety (ADR 0001): a refresh failure that requires a login is written to ``auth.json`` as
``refresh_blocked`` for the current generation, the K3 limit as ``not_before`` and
``refresh_posts``. These writes keep the generation and the tokens. After a restart no POST is sent
while the file's generation is blocked or ``not_before`` lies ahead.

Shutdown contract: the owner awaits ``aclose()`` in the ``finally`` of ``main`` before it closes the
``ClientSession``, so a running POST and its write can finish. A failed refresh task is logged with
its type by a done callback, so no exception stays unretrieved.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from aiohttp import ClientSession
from homecom_alt import AuthFailedError, ConnectionOptions, HomeComAlt

from ..logging_setup import add_secret
from .claims import persistable_exp, plausible_exp, token_times
from .refresh_errors import Action, RefreshClass, classify, retry_after
from .store import (
    AuthState,
    AuthStorageError,
    AuthStore,
    AuthStoreError,
    AuthUpdate,
    InvalidAuthFileError,
    LockTimeoutError,
    RefreshMarks,
)

STARTING = "starting"
READY = "ready"
DISCONNECTED = "disconnected"
AUTH_REQUIRED = "auth_required"
AUTH_REQUIRED_CODE = "AUTH_REQUIRED"  # error event code (PLAN.md, MQTT contract)

AUTH_CHECK_INTERVAL = 10.0
CRASH_LOOP_GUARD = 60.0
MARGIN_EXTRA = 60.0
BACKOFF_START = 30.0
BACKOFF_MAX = 900.0
K3_WINDOW = 3600.0
K3_MAX_POSTS = 3
# flock is not reliable across hosts on these; every fuse.* type is warned about as well.
NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smb", "smb3", "smbfs", "ceph", "glusterfs", "9p", "virtiofs", "afs"}
)
GUARD = "guard"  # outcome of an attempt that waits for the crash-loop guard; no state change

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class Phase(enum.Enum):
    IDLE = "idle"
    ACQUIRING = "acquiring"
    BACKOFF = "backoff"
    SENDING = "sending"
    WRITING = "writing"


class TokenManagerClosedError(Exception):
    """``aclose()`` was called; no further refresh is started."""


def network_filesystem(path: Path, mounts: str = "/proc/mounts") -> str | None:
    """File system type of ``path``'s directory if it is a network or FUSE file system, else ``None``."""
    try:
        lines = Path(mounts).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    target = str(Path(path).parent.resolve())
    best, fs_type = "", None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1])
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and len(point) >= len(best):
            best, fs_type = point, fields[2]
    if fs_type is not None and (fs_type in NETWORK_FILESYSTEMS or fs_type.startswith("fuse")):
        return fs_type
    return None


def _blocked(state: AuthState) -> bool:
    """A refresh failure requires a login for exactly this generation."""
    return state.refresh_blocked is not None and state.blocked_generation == state.generation


def _usable(token: object) -> bool:
    return isinstance(token, str) and bool(token)


def _timestamp(value: str | None) -> float | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if parsed is None:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


class TokenManager:
    """One instance per process and ``auth.json``."""

    def __init__(
        self,
        store: AuthStore,
        session: ClientSession,
        *,
        brand: str,
        poll_timeout: float,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_state: Callable[[str], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        mounts: str = "/proc/mounts",
    ) -> None:
        self._store = store
        self._session = session
        self._brand = brand
        self._poll_timeout = poll_timeout
        self._clock = clock
        self._sleep = sleep
        self._on_state = on_state
        self._on_error = on_error
        # ConnectionOptions.auth_provider is unused by homecom_alt 1.8.2 (model.py:14); only the
        # constructor flag decides whether get_token() refreshes. It is set to False explicitly, and
        # a test makes sure the refresh instance still refreshes with it.
        self._options = ConnectionOptions(brand=brand, auth_provider=False)
        self._refresh_api = HomeComAlt(session, self._options, auth_provider=True)
        self.state = STARTING
        self.phase = Phase.IDLE
        self.last_failure: RefreshClass | None = None
        self._identity: tuple[int, str | None, str] | None = None  # (generation, login_id, refresh token)
        self._file_exp: tuple[str | None, int | None] = (None, None)  # access token and exp of the file
        self._last_refresh_at: str | None = None
        self._obtained_at = clock()
        self._unpersisted: AuthUpdate | None = None
        self._pending_marks: RefreshMarks | None = None
        self._posts: list[float] = []  # refresh POSTs actually sent within the K3 window
        self._not_before = 0.0
        self._hold_until = 0.0  # no refresh before this time: a token just obtained was already due
        self._skew_warned = False
        self._task: asyncio.Task[None] | None = None
        self._guard_pending = True
        self._margin_warned = False
        self._closed = False
        fs_type = network_filesystem(store.path, mounts)
        if fs_type:
            _LOGGER.warning(
                "%s is on %s; flock is only reliable on local volumes, a parallel login may be lost",
                store.path.parent,
                fs_type,
            )

    def fetch_api(self, cls: type[HomeComAlt] = HomeComAlt, **kwargs: Any) -> HomeComAlt:
        """Instance for discovery and data requests; it never refreshes (``auth_provider=False``)."""
        return cls(self._session, self._options, auth_provider=False, **kwargs)

    # Public entry points

    async def ensure_fresh(self) -> None:
        """Run before every discovery and poll.

        Reads the file under the lock on every call (or joins the running refresh task, which
        holds the lock itself), adopts a changed file, retries a pending write, and refreshes if
        the access token is missing or due (remaining lifetime below the margin). A file whose
        generation is blocked leads to ``auth_required`` without a POST.
        """
        self._check_open()
        if self._busy():
            await self._join(self._task)
            return
        if self._unpersisted is not None:
            await self._persist_pending()
        else:
            try:
                current = await self._store.read_locked()
            except AuthStoreError as error:
                _LOGGER.warning("Reading the auth file failed (%s); using the tokens in memory", error)
            else:
                if current is not None and self._changed(current):
                    self._adopt(current)
                if current is None or _blocked(current):
                    if current is not None:
                        _LOGGER.error("Refresh is blocked by %s; run login", current.refresh_blocked)
                    await self._join(self._start(wait_for_login=True))
                    return
        if self._fresh_enough() or self._clock() < self._hold_until:
            self._set_state(READY)
        else:
            await self._join(self._start())

    async def refresh_locked(self, rejected_token: str | None = None) -> None:
        """Start the refresh task (or join the running one) and wait for fresh tokens."""
        self._check_open()
        await self._refresh(rejected_token)

    async def on_unauthorized(self, used_token: str | None) -> None:
        """A fetch instance got 401 with ``used_token``: exactly one locked refresh."""
        self._check_open()
        if self._options.token != used_token and self._fresh_enough():
            return  # another refresh already replaced the rejected token
        await self.refresh_locked(rejected_token=used_token)

    async def run_poll(self, poll: Callable[[], Awaitable[T]]) -> T:
        """``ensure_fresh()``, then ``poll()`` under the poll timeout; refreshes stay outside it.

        A 401 triggers one refresh and one repetition. A second 401 means ``auth_required`` if
        the used token was still valid by ``exp``; otherwise ``ensure_fresh()`` runs once more.
        """
        await self.ensure_fresh()
        refreshed = expired_retry = False
        while True:
            used = self._options.token
            try:
                async with asyncio.timeout(self._poll_timeout):
                    return await poll()
            except AuthFailedError:
                pass
            if not refreshed:
                refreshed = True
                await self.on_unauthorized(used)
            elif not expired_retry and not self._valid_by_exp(used):
                expired_retry = True
                await self.ensure_fresh()
            else:
                _LOGGER.error("Fresh access token rejected twice; a new login is required")
                await self._refresh(used, wait_for_login=True)
                refreshed = expired_retry = False

    async def aclose(self) -> None:
        """Stop: cancel waiting (nothing in flight), but wait for a running POST and its write.

        Await it before closing the ``ClientSession`` (shutdown contract, ADR 0001).
        """
        self._closed = True
        task = self._task
        if task is None or task.done():
            return
        if self.phase in (Phase.IDLE, Phase.ACQUIRING, Phase.BACKOFF):
            task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved here; joiners still get it

    # Refresh task

    def _busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _refresh(self, rejected: str | None, wait_for_login: bool = False) -> None:
        """Start the refresh task or join the running one.

        A joined task started for another reason may end with the rejected token still in use; then
        this request runs once more in a task of its own.
        """
        foreign = self._busy()
        await self._join(self._start(rejected=rejected, wait_for_login=wait_for_login))
        if foreign and rejected is not None and self._options.token == rejected:
            await self._join(self._start(rejected=rejected, wait_for_login=wait_for_login))

    def _start(self, *, rejected: str | None = None, wait_for_login: bool = False) -> asyncio.Task[None]:
        self._check_open()
        if not self._busy():
            self.phase = Phase.IDLE
            self._task = asyncio.create_task(self._run(rejected, wait_for_login))
            self._task.add_done_callback(_log_task_failure)
        assert self._task is not None
        return self._task

    async def _join(self, task: asyncio.Task[None] | None) -> None:
        assert task is not None
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current and current.cancelling()):
                raise TokenManagerClosedError("token manager closed") from None
            raise

    async def _run(self, rejected: str | None, wait_for_login: bool) -> None:
        backoff = BACKOFF_START
        try:
            while True:
                if wait_for_login:
                    await self._wait_for_login()
                    wait_for_login, rejected, backoff = False, None, BACKOFF_START
                    if self._fresh_enough():
                        self._set_state(READY)
                        return
                    self._set_state(STARTING)  # a renewed failure is a new auth_required event
                self._check_open()
                outcome, delay = await self._attempt(rejected)
                if outcome is None:
                    self._hold_if_due()
                    self._set_state(READY)
                    return
                if outcome is Action.AUTH_REQUIRED:
                    wait_for_login = True
                    continue
                if outcome == GUARD:
                    _LOGGER.warning("Last refresh was less than %.0f s ago; waiting %.0f s", CRASH_LOOP_GUARD, delay)
                    await self._backoff(delay)
                    continue
                self._set_state(DISCONNECTED)
                if outcome is Action.RETRY_BACKOFF:
                    delay, backoff = backoff, min(backoff * 2, BACKOFF_MAX)
                _LOGGER.warning("Next refresh attempt in %.0f s", delay)
                adopted = await self._wait_watching(delay)
                if adopted is None:
                    continue
                rejected, backoff = None, BACKOFF_START
                if _blocked(adopted):
                    wait_for_login = True
                elif self._fresh_enough():
                    self._set_state(READY)
                    return
        finally:
            self.phase = Phase.IDLE

    async def _attempt(self, rejected: str | None) -> tuple[Action | str | None, float]:
        """One attempt under the lock: ``(outcome, delay)``; outcome ``None`` means fresh tokens."""
        result: dict[str, Any] = {}

        async def refresh(current: AuthState | None) -> AuthUpdate | RefreshMarks | None:
            # First statement after LOCK_NB succeeded, no await in between: from here on the
            # attempt is awaited by aclose(), never cancelled.
            self.phase = Phase.SENDING
            result["ran"] = True
            if current is None:
                _LOGGER.error("%s does not exist; run login", self._store.path)
                result["outcome"] = Action.AUTH_REQUIRED
                return None
            adopted = self._changed(current)
            if adopted:
                self._adopt(current)  # read first: never POST with a token a login replaced
            if _blocked(current):
                _LOGGER.error("Refresh is blocked by %s; run login", current.refresh_blocked)
                result["outcome"] = Action.AUTH_REQUIRED
                return None
            if adopted and self._fresh_enough() and self._options.token != rejected:
                return None
            now = self._clock()
            self._posts = sorted({t for t in (*self._posts, *current.refresh_posts) if now - K3_WINDOW < t <= now})
            self._posts = self._posts[-K3_MAX_POSTS:]
            wait = max(self._not_before, current.not_before or 0.0) - now
            if 0 < wait <= K3_WINDOW:  # K3 from before a restart; a wait beyond the window is implausible
                result["outcome"], result["delay"] = Action.RETRY_AFTER, wait
                return None
            if self._guard_pending:
                self._guard_pending = False  # consumed only once last_refresh_at is known
                wait = self._guard_wait(current.last_refresh_at, now)
                if wait > 0:
                    result["outcome"], result["delay"] = GUARD, wait
                    return None
            sent, before = self._options.refresh_token, self._options.token
            self._posts.append(now)
            try:
                ok = await self._refresh_api.get_token(force=True)
            except Exception as error:  # K10 catches Exception only, never BaseException
                self._options.token, self._options.refresh_token = before, sent
                cls = classify(error)
                if cls is RefreshClass.K4:
                    self._posts.pop()  # provably not sent: does not count towards the K3 limit
                return self._failed(result, cls, type(error).__name__, error)
            access, new_refresh = self._options.token, self._options.refresh_token
            if ok is not True or not _usable(access) or not _usable(new_refresh):
                self._options.token, self._options.refresh_token = before, sent
                if ok is not True:
                    return self._failed(result, RefreshClass.K0, type(ok).__name__, None)
                bad = access if not _usable(access) else new_refresh
                return self._failed(result, RefreshClass.K9, type(bad).__name__, None)
            add_secret(access)
            add_secret(new_refresh)
            now = self._clock()
            self._obtained_at, self._last_refresh_at = now, _iso(now)
            # Secure the new tokens before anything else touches them (claims parsing below).
            update = AuthUpdate(new_refresh, current.brand, access, None, _iso(now), refresh_posts=tuple(self._posts))
            self._unpersisted = result["update"] = update
            if new_refresh == sent:
                _LOGGER.warning("Refresh returned the same refresh token (no rotation); keeping it")
            update = replace(update, exp=persistable_exp(access, now))
            self._unpersisted = result["update"] = update
            self.phase = Phase.WRITING
            return update

        self.phase = Phase.ACQUIRING
        try:
            state = await self._store.locked_update(refresh)
        except LockTimeoutError as error:
            _LOGGER.warning("Refresh not sent (%s); handled like K4", error)
            return Action.RETRY_BACKOFF, 0.0
        except InvalidAuthFileError as error:
            _LOGGER.error("%s", error)
            return Action.AUTH_REQUIRED, 0.0
        except (AuthStorageError, ValueError) as error:
            if "update" in result:
                # M1: keep running with the tokens in memory; the write is retried before every
                # poll, and no further refresh starts unless the access token is due.
                _LOGGER.error("Refreshed tokens could not be written (%s); retrying before every poll", error)
                return None, 0.0
            if "marks" in result:
                # The POST was sent: never handled like K4. Retried while waiting (auth_required).
                self._pending_marks = result["marks"]
                _LOGGER.error("Refresh state could not be written (%s); a restart may refresh once more", error)
                return result["outcome"], result.get("delay", 0.0)
            _LOGGER.warning("Refresh not sent (%s); handled like K4", error)
            return Action.RETRY_BACKOFF, 0.0
        if "update" in result:
            self._known(state)  # type: ignore[arg-type]
            self._unpersisted = None
            exp = result["update"].exp
            valid = f", access token valid for {int(exp - self._obtained_at)} s" if exp else ""
            _LOGGER.info("Tokens refreshed (generation %d%s)", state.generation, valid)  # type: ignore[union-attr]
        if "update" in result or "marks" in result:
            self._pending_marks = None  # the file now holds newer bookkeeping
        outcome = result.get("outcome")
        if outcome is None:
            self.last_failure = None
        return outcome, result.get("delay", 0.0)

    def _failed(
        self, result: dict[str, Any], cls: RefreshClass, type_name: str, error: Exception | None
    ) -> RefreshMarks | None:
        """Record a failed POST; returns the marks to write under the same lock (none for K4)."""
        self.last_failure = cls
        _LOGGER.error("Refresh failed: %s (%s), %s", cls.name, cls.rotation, type_name)
        result["outcome"] = cls.action
        if cls.action is Action.RETRY_BACKOFF:
            return None
        if cls.action is Action.RETRY_AFTER:
            now = self._clock()
            delay = self._k3_delay(error, now)
            self._not_before, result["delay"] = now + delay, delay
            marks = RefreshMarks(None, self._not_before, tuple(self._posts))
        else:
            marks = RefreshMarks(cls.name, None, tuple(self._posts))
        result["marks"] = marks
        return marks

    async def _persist_pending(self) -> None:
        pending = self._unpersisted

        async def write(current: AuthState | None) -> AuthUpdate | None:
            if self._unpersisted is not pending:
                return None  # a refresh replaced or wrote it meanwhile; never write a stale pair
            if current is not None and self._changed(current):
                self._adopt(current)  # a login won; the unpersisted refresh result is dropped
                return None
            return pending

        try:
            state = await self._store.locked_update(write)
        except (AuthStoreError, ValueError) as error:
            _LOGGER.error("Refreshed tokens are still not written (%s); retrying before the next poll", error)
            return
        if self._unpersisted is pending and state is not None:
            self._known(state)
            self._unpersisted = self._pending_marks = None
            _LOGGER.info("Refreshed tokens written (generation %d)", state.generation)

    async def _wait_for_login(self) -> None:
        """``auth_required``: every 10 s read the file, adopt a new login, write pending marks."""
        self._check_open()
        self._set_state(AUTH_REQUIRED)
        while True:
            await self._backoff(AUTH_CHECK_INTERVAL)
            adopted = await self._check_file()
            if adopted is not None and not _blocked(adopted):
                return

    async def _wait_watching(self, delay: float) -> AuthState | None:
        """Wait ``delay`` in steps of ``AUTH_CHECK_INTERVAL``, checking the file between the steps.

        Returns the adopted state as soon as the file changed (e.g. a login), else ``None``.
        """
        remaining = delay
        while remaining > 0:
            step = min(remaining, AUTH_CHECK_INTERVAL)
            await self._backoff(step)
            remaining -= step
            if remaining > 0:
                known = self._identity is not None  # a first read (e.g. after a lock timeout) is no login
                adopted = await self._check_file()
                if adopted is not None and known:
                    return adopted
        return None

    async def _check_file(self) -> AuthState | None:
        """Read the file under the lock: adopt and return a changed file, else write pending marks."""
        self.phase = Phase.ACQUIRING
        seen: dict[str, Any] = {}

        async def check(current: AuthState | None) -> RefreshMarks | None:
            if current is not None and self._changed(current):
                self._adopt(current)
                seen["adopted"] = current
                return None
            if current is None or self._pending_marks is None:
                return None
            seen["marks"] = self._pending_marks
            return self._pending_marks

        try:
            await self._store.locked_update(check)
        except (AuthStoreError, ValueError) as error:
            _LOGGER.debug("Checking the auth file failed (%s)", error)
            return None
        if "marks" in seen and self._pending_marks is seen["marks"]:
            self._pending_marks = None
        return seen.get("adopted")

    def _hold_if_due(self) -> None:
        """A token just obtained is already due (e.g. the clock runs ahead): no refresh for a while."""
        if self._fresh_enough():
            return
        if not self._skew_warned:
            self._skew_warned = True
            _LOGGER.warning(
                "The new access token is already due; is the local clock ahead? Refreshing at most every %.0f s",
                CRASH_LOOP_GUARD,
            )
        self._hold_until = self._clock() + CRASH_LOOP_GUARD

    def _guard_wait(self, last_refresh_at: str | None, now: float) -> float:
        """Crash-loop guard: wait until the last refresh is 60 s ago, never longer than 60 s."""
        last = _timestamp(last_refresh_at)
        return 0.0 if last is None else min(max(last + CRASH_LOOP_GUARD - now, 0.0), CRASH_LOOP_GUARD)

    async def _backoff(self, delay: float) -> None:
        self._check_open()
        self.phase = Phase.BACKOFF
        await self._sleep(delay)
        self._check_open()

    def _k3_delay(self, error: Exception | None, now: float) -> float:
        delay = retry_after(error, now) if error is not None else BACKOFF_START
        if len(self._posts) >= K3_MAX_POSTS:  # the oldest of the last three POSTs opens the window
            delay = max(delay, self._posts[-K3_MAX_POSTS] + K3_WINDOW - now)
        return delay

    # Helpers

    def _check_open(self) -> None:
        if self._closed:
            raise TokenManagerClosedError("token manager closed")

    def _changed(self, state: AuthState) -> bool:
        return (state.generation, state.login_id, state.refresh_token) != self._identity

    def _known(self, state: AuthState) -> None:
        """Remember the file as this process last read or wrote it."""
        self._identity = (state.generation, state.login_id, state.refresh_token)
        self._file_exp = (state.access_token, state.exp)

    def _adopt(self, state: AuthState) -> None:
        if self._identity is None or state.login_id != self._identity[1]:
            # A new login is a new identity: new K3 window, and no hold from the previous tokens.
            self._posts, self._not_before, self._hold_until = [], 0.0, 0.0
        add_secret(state.refresh_token)
        add_secret(state.access_token)
        self._options.token, self._options.refresh_token = state.access_token, state.refresh_token
        self._brand = state.brand
        self._known(state)
        self._last_refresh_at = state.last_refresh_at
        now = self._clock()
        self._obtained_at = min(_timestamp(state.updated_at) or now, now)
        self._unpersisted = None
        self._pending_marks = None
        _LOGGER.info("Using generation %d from %s", state.generation, self._store.path)

    def _times(self, token: str | None) -> tuple[float, float | None] | None:
        """``(exp, iat)`` of ``token``; the persisted ``exp`` is the fallback for the file's token."""
        now = self._clock()
        times = token_times(token, now)
        file_token, file_exp = self._file_exp
        if times is None and token is not None and token == file_token and file_exp is not None:
            try:
                exp = float(file_exp)
            except OverflowError:
                return None
            return (exp, None) if plausible_exp(exp, now) else None
        return times

    def _valid_by_exp(self, token: str | None) -> bool:
        times = self._times(token)
        return times is not None and times[0] > self._clock()

    def _fresh_enough(self) -> bool:
        """Remaining lifetime >= margin = min(poll timeout + 60 s, lifetime / 2)."""
        times = self._times(self._options.token)
        if times is None:
            return False
        exp, iat = times
        half_lifetime = (exp - (iat if iat is not None else self._obtained_at)) / 2
        if half_lifetime <= 0:
            return False  # expired when obtained, or a clock far off
        if self._poll_timeout + MARGIN_EXTRA > half_lifetime and not self._margin_warned:
            self._margin_warned = True
            _LOGGER.warning(
                "Poll timeout + %.0f s exceeds half the token lifetime (%.0f s); a 401 during a poll "
                "stays possible (availability only)",
                MARGIN_EXTRA,
                half_lifetime * 2,
            )
        return exp - self._clock() >= min(self._poll_timeout + MARGIN_EXTRA, half_lifetime)

    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        _LOGGER.info("Token state: %s", state)
        if self._on_state:
            self._on_state(state)
        if state == AUTH_REQUIRED and self._on_error:
            self._on_error(AUTH_REQUIRED_CODE, "Refresh token missing, rejected or lost; run login")


def _log_task_failure(task: asyncio.Task[None]) -> None:
    """Done callback: retrieve and log a failure of the refresh task (type only, no message)."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None and not isinstance(error, TokenManagerClosedError):
        _LOGGER.error("Refresh task failed: %s", type(error).__name__)
