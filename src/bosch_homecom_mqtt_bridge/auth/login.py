"""Interactive browser login for the ``login`` subcommand.

Flow: own PKCE verifier and ``state`` (memory only) -> login URL -> code or redirect URL from
stdin -> token exchange via ``validate_auth`` -> write ``auth.json`` -> list gateways.

Code, verifier and tokens are never printed or logged; the login URL (``state`` and challenge)
may be shown. ``ConnectionOptions.code`` is never set, because ``get_token()`` would then redeem
it with the library's fixed public verifier.
"""
from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import inspect
import os
import re
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TextIO
from urllib.parse import quote, unquote, urlencode, urlsplit

from aiohttp import ClientError, ClientSession
from homecom_alt import (
    ApiError,
    AuthFailedError,
    ConnectionOptions,
    HomeComAlt,
    NotRespondingError,
)
from homecom_alt.const import OAUTH_DOMAIN, OAUTH_LOGIN, OAUTH_LOGIN_PARAMS, OAUTH_LOGIN_PARAMS_BUDERUS
from tenacity import RetryError

from ..config import Config
from ..logging_setup import add_secret
from .claims import persistable_exp, token_times
from .store import AuthState, AuthStore, AuthStoreError, AuthUpdate, InvalidAuthFileError, LockTimeoutError

EXIT_OK = 0
EXIT_FAILED = 1

# Server strings are shown only if they look like an identifier; anything else is shown as repr.
_PRINTABLE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


class LoginError(Exception):
    """A login step failed; the message is meant for the user and never contains secrets."""


def new_pkce() -> tuple[str, str]:
    """Return a fresh ``(code_verifier, S256 code_challenge)`` pair."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def login_url(brand: str, state: str, challenge: str) -> str:
    """Login URL for ``brand``; it must match the brand used for the token exchange."""
    params = OAUTH_LOGIN_PARAMS_BUDERUS if brand == "buderus" else OAUTH_LOGIN_PARAMS
    query = urlencode({**params, "code_challenge": challenge, "state": state})
    return f"{OAUTH_DOMAIN}{OAUTH_LOGIN}?{query}"


def extract_code(text: str, expected_state: str) -> str:
    """Return the URL-encoded code from a bare code or a redirect URL.

    For a URL, ``state`` must match this login. The library puts the code into the form body
    unencoded, hence the encoding here.
    """
    text = text.strip()
    if not text:
        raise LoginError("Keine Eingabe erhalten.")
    if "://" in text or "code=" in text:
        query = urlsplit(text).query if "://" in text else text.lstrip("?")
        params: dict[str, list[str]] = {}
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            # unquote, not unquote_plus: a literal "+" in the code must survive.
            params.setdefault(unquote(key), []).append(unquote(value))
        codes = params.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise LoginError("Die Weiterleitungs-URL enthält keinen eindeutigen Code.")
        if params.get("state") != [expected_state]:
            raise LoginError("Der state der URL gehört nicht zu diesem Login. Bitte den Login neu starten.")
        code = codes[0]
    else:
        code = unquote(text)
    return quote(code, safe="")


async def _exchange_code(api: HomeComAlt, code: str, verifier: str) -> tuple[str, str]:
    """Redeem ``code`` and return ``(access_token, refresh_token)``."""
    try:
        data = await api.validate_auth(code, verifier)
    except (AttributeError, AuthFailedError):
        # 400 from the token endpoint arrives as None (403/404/502/504 as {}) and fails inside
        # validate_auth with AttributeError; 401 and non-JSON bodies raise AuthFailedError.
        raise LoginError("Code ungültig, abgelaufen oder abgelehnt. Bitte erneut einloggen.") from None
    except (NotRespondingError, ApiError, ClientError, TimeoutError) as error:
        # TimeoutError: reading the response body is outside the library's timeout handling.
        raise LoginError(
            f"Token-Austausch fehlgeschlagen ({type(error).__name__}). Bitte erneut einloggen."
        ) from None
    access = data.get("access_token") if isinstance(data, dict) else None
    refresh = data.get("refresh_token") if isinstance(data, dict) else None
    if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
        raise LoginError("Der Token-Austausch lieferte keine Tokens. Bitte erneut einloggen.")
    return access, refresh


def _show(value: object) -> str:
    """Server-supplied value for the terminal: plain if identifier-like, otherwise as repr."""
    if isinstance(value, str) and _PRINTABLE_ID.fullmatch(value):
        return value
    return repr(value)


def _print_lifetime(access_token: str | None, out: TextIO) -> None:
    times = token_times(access_token)
    if times is None:
        print("Restlaufzeit des Access-Tokens unbekannt (kein gültiges exp).", file=out)
        return
    remaining = int(times[0] - datetime.now(UTC).timestamp())
    print(f"Access-Token gültig noch {remaining} s.", file=out)


async def run_login(
    store: AuthStore,
    brand: str,
    session: ClientSession,
    *,
    read_input: Callable[[str], str],
    out: TextIO,
    err: TextIO,
) -> int:
    """Run the interactive login and return the exit code."""
    directory = store.path.parent
    if not (directory.is_dir() and os.access(directory, os.W_OK)):
        print(f"Verzeichnis {directory} fehlt oder ist nicht beschreibbar.", file=err)
        return EXIT_FAILED

    verifier, challenge = new_pkce()
    add_secret(verifier)
    state = secrets.token_urlsafe(16)
    print("Diese URL im Browser öffnen und anmelden:", file=out)
    print(login_url(brand, state, challenge), file=out)
    print(
        "Der Browser endet auf einer Adresse, die er nicht öffnen kann (…://app/login?code=…). "
        "Diese Adresse oder nur den Code aus Adresszeile bzw. Entwicklerkonsole kopieren.",
        file=out,
    )
    try:
        code = extract_code(read_input("Code oder Weiterleitungs-URL: "), state)
    except (EOFError, KeyboardInterrupt):
        print("Login abgebrochen.", file=err)
        return EXIT_FAILED
    except LoginError as error:
        print(error, file=err)
        return EXIT_FAILED
    # The filter ignores values shorter than MIN_RUNTIME_SECRET_LENGTH, so a short code cannot
    # turn ordinary words in later log lines into "***".
    add_secret(code)
    add_secret(unquote(code))

    options = ConnectionOptions(brand=brand)
    # auth_provider=False: get_token() is a no-op on this instance, so it never refreshes.
    api = HomeComAlt(session, options, auth_provider=False)

    issued = False

    async def exchange(_current: AuthState | None) -> AuthUpdate:
        nonlocal issued
        access, refresh = await _exchange_code(api, code, verifier)
        issued = True
        add_secret(access)
        add_secret(refresh)
        options.token = access
        options.refresh_token = refresh
        # D7: the access token is persisted too, so a restart can use it without a refresh. A new
        # random login_id marks the file as a new login even if its generation starts over at 1.
        # The token manager's refresh marks are not passed on: a login clears them.
        return AuthUpdate(
            refresh_token=refresh,
            brand=brand,
            access_token=access,
            exp=persistable_exp(access),
            login_id=secrets.token_hex(16),
        )

    # The store lock is taken before the code is redeemed. A lock timeout therefore aborts before
    # validate_auth: no token pair has been issued, so nothing can be lost, and the user just
    # starts the login again. Once the exchange succeeded the lock is already held, so writing the
    # tokens cannot time out; a storage error at that point loses the issued pair, which the
    # message says (``issued``). Login does not set last_refresh_at: only the service refresh (crash-
    # loop guard of the token manager) needs it, and the store carries the previous value over.
    try:
        saved = await store.locked_update(exchange)
    except LockTimeoutError:
        print(
            f"{store.lock_path} ist durch einen anderen Prozess gesperrt. Der Code wurde nicht "
            "eingelöst; bitte den Login erneut starten.",
            file=err,
        )
        return EXIT_FAILED
    except LoginError as error:
        print(error, file=err)
        return EXIT_FAILED
    except InvalidAuthFileError:
        # Raised while reading, before the code is redeemed (also for a non-regular auth.json).
        print(
            f"{store.path} ist keine gültige Auth-Datei. Datei sichern und entfernen, dann den Login "
            "erneut starten. Der Code wurde nicht eingelöst.",
            file=err,
        )
        return EXIT_FAILED
    except AuthStoreError as error:
        if issued:
            print(f"Token ausgestellt, aber nicht gespeichert ({error}). Bitte erneut einloggen.", file=err)
        else:
            print(
                f"Speicherfehler ({error}). Der Code wurde nicht eingelöst; bitte den Login erneut starten.",
                file=err,
            )
        return EXIT_FAILED

    print(f"Login gespeichert in {store.path} (Generation {saved.generation}).", file=out)
    _print_lifetime(options.token, out)

    print("Prüfe Gateways … (bei Zeitüberschreitung mit Wiederholungen)", file=out)
    try:
        devices = await api.async_get_devices()
        if inspect.isawaitable(devices):  # 1.8.2 returns the unawaited response.json() coroutine
            devices = await devices
    except RetryError as error:
        # tenacity gives up after five NotRespondingError attempts (timeout, 429, no connection).
        cause = error.last_attempt.exception()
        print(f"Login gespeichert, Gateway-Abfrage fehlgeschlagen ({type(cause).__name__}).", file=err)
        return EXIT_FAILED
    except asyncio.CancelledError:
        # Ctrl+C under asyncio.run arrives here as cancellation; the login itself is safe.
        print("Login gespeichert, Gateway-Abfrage abgebrochen.", file=err)
        raise
    except (Exception, KeyboardInterrupt) as error:
        # AuthFailedError, ApiError, InvalidSensorDataError, ClientError, AttributeError (403/404/
        # 502/504 arrive as {} and fail on {}.json() inside the library), ValueError for a broken
        # JSON body, TimeoutError while reading it, and anything else: the token is already saved.
        print(f"Login gespeichert, Gateway-Abfrage fehlgeschlagen ({type(error).__name__}).", file=err)
        return EXIT_FAILED
    if not isinstance(devices, list):
        print("Login gespeichert, Gateway-Abfrage lieferte eine unerwartete Antwort.", file=err)
        return EXIT_FAILED
    if not devices:
        print("Login gespeichert, aber das Konto hat keine Gateways.", file=err)
        return EXIT_FAILED
    for device in devices:
        device = device if isinstance(device, dict) else {}
        print(f"Gateway {_show(device.get('deviceId'))}: Typ {_show(device.get('deviceType'))}", file=out)
    return EXIT_OK


async def login(config: Config) -> int:
    """Entry point of the ``login`` subcommand."""
    store = AuthStore(config.bosch_auth_path)
    async with ClientSession() as session:
        return await run_login(
            store,
            config.bosch_brand,
            session,
            read_input=getpass.getpass,  # no echo: the code is a secret
            out=sys.stdout,
            err=sys.stderr,
        )
