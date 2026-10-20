"""Browser login over MQTT (``MQTT_LOGIN_ENABLED``, MQTT contract).

``start`` on ``<base>/bridge/cmd/login`` creates a PKCE verifier and ``state`` (memory only) and
publishes the login URL retained on ``<base>/bridge/login``. Any other payload is the redirect URL, the
bare code or a whole browser console line containing the URL; its code is redeemed through the same
``redeem_code`` as the terminal login, so ``auth.json`` gets a new login that the token manager adopts
within about 10 s.

At most one login is pending; it expires after ``LOGIN_LIFETIME`` seconds. A pending login ends with the
first input that carries a code for it, successful or not, because the code may already be spent.
Retained commands are ignored: a code kept by the broker would be replayed on every connect. Payloads,
codes and the verifier are never logged; error events name only the failed step.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

from aiohttp import ClientSession

from .auth.login import IssuedNotSavedError, LoginError, extract_code, login_api, login_url, new_pkce, redeem_code
from .auth.store import AuthStore, AuthStoreError, InvalidAuthFileError, LockTimeoutError
from .logging_setup import add_secret
from .publisher import MqttPublisher

_LOGGER = logging.getLogger(__name__)

LOGIN_FAILED = "LOGIN_FAILED"
LOGIN_LIFETIME = 600.0  # seconds a started login waits for its code
CLOSE_TIMEOUT = 15.0  # a running code exchange may finish on shutdown (request timeout 15 s)
START = "start"
# A redirect URL or console line is well below this; longer payloads are refused before any parsing.
MAX_PAYLOAD = 4096


class MqttLogin:
    def __init__(
        self,
        store: AuthStore,
        brand: str,
        session: ClientSession,
        publisher: MqttPublisher,
        lifetime: float = LOGIN_LIFETIME,
    ) -> None:
        self._store = store
        self._brand = brand
        self._session = session
        self._publisher = publisher
        self._lifetime = lifetime
        self._pending: tuple[str, str] | None = None  # (state, verifier)
        self._expiry: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        """Clear a login URL a previous run left in the broker; its verifier is gone."""
        self._publisher.on_login_command(self.handle)
        self._publisher.publish_login("idle")

    def handle(self, payload: bytes, retained: bool) -> None:
        if self._closed:
            return  # shutting down: no new exchange may start once aclose() ran
        if retained:
            _LOGGER.warning("Ignoring a retained login command; send it without the retain flag")
            return
        if len(payload) > MAX_PAYLOAD:
            self._fail(f"the command is longer than {MAX_PAYLOAD} bytes")
            return
        text = payload.decode("utf-8", "replace").strip()
        if not text:
            _LOGGER.warning("Ignoring an empty login command")
        elif text.lower() == START:
            self._start()
        else:
            self._submit(text)

    def _busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def _fail(self, message: str) -> None:
        _LOGGER.warning("MQTT login: %s", message)
        self._publisher.publish_error(LOGIN_FAILED, message)

    def _start(self) -> None:
        if self._busy():
            self._fail("a code is being redeemed; try again later")
            return
        self._clear()
        verifier, challenge = new_pkce()
        add_secret(verifier)
        state = secrets.token_urlsafe(16)
        self._pending = (state, verifier)
        self._expiry = asyncio.get_running_loop().call_later(self._lifetime, self._expire)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._lifetime)
        self._publisher.publish_login("pending", login_url(self._brand, state, challenge), expires_at)
        _LOGGER.info("MQTT login started; waiting %d s for the code", int(self._lifetime))

    def _expire(self) -> None:
        self._expiry = None
        if self._pending is not None:
            self._pending = None
            self._publisher.publish_login("idle")
            _LOGGER.info("MQTT login expired without a code")

    def _clear(self) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        self._pending = None

    def _submit(self, text: str) -> None:
        if self._busy():
            self._fail("a code is being redeemed; try again later")
            return
        if self._pending is None:
            self._fail("no login pending; send start first")
            return
        state, verifier = self._pending
        try:
            code = extract_code(text, state)
        except LoginError:
            # Nothing was redeemed, so the login stays pending for a corrected input.
            self._fail("the input holds no code for the pending login")
            return
        add_secret(code)
        add_secret(unquote(code))
        self._clear()
        self._task = asyncio.create_task(self._redeem(code, verifier), name="mqtt-login")

    async def _redeem(self, code: str, verifier: str) -> None:
        api, options = login_api(self._session, self._brand)
        try:
            saved = await redeem_code(self._store, api, options, code, verifier)
        except IssuedNotSavedError:
            message = "tokens were issued but could not be saved; start the login again"
        except LockTimeoutError:
            message = "the auth file is locked; the code was not redeemed, start the login again"
        except InvalidAuthFileError:
            message = "the auth file is invalid; the code was not redeemed"
        except AuthStoreError:
            message = "the auth file could not be read; the code was not redeemed"
        except LoginError:
            message = "the code was rejected or the exchange failed; start the login again"
        except Exception as error:  # noqa: BLE001 - reported by type name only
            message = f"login failed ({type(error).__name__})"
        else:
            self._publisher.publish_login("done")
            _LOGGER.info("MQTT login saved (generation %d)", saved.generation)
            return
        self._publisher.publish_login("idle")
        self._fail(message)

    async def aclose(self, timeout: float = CLOSE_TIMEOUT) -> None:
        """Drop a pending login; let a running code exchange and its write finish within ``timeout``."""
        self._closed = True
        if self._pending is not None:
            self._publisher.publish_login("idle")  # the verifier dies with the process
        self._clear()
        task = self._task
        if task is None or task.done():
            return
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            _LOGGER.warning("MQTT login exchange cancelled on shutdown; the code may be lost")
