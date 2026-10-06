"""Classification of refresh failures (table K, ADR 0001) and the chosen retry policy D11 (b).

Only the refresh instance (``auth_provider=True``) sends the refresh POST, so every exception
caught around ``get_token(force=True)`` belongs to the token endpoint; a 401 there is K2, never an
expired bearer of a data request.

D11 option (b): retry only when the POST was not processed: K3 (429, assumption) and K4
(provably not sent). Every other class needs a new login (``auth_required``).

Timeouts: aiohttp raises ``ConnectionTimeoutError`` only while the connector is still connecting
(``client.py``, ``_connect_and_send_request``), before ``req.send``; that is K4. A read timeout
(``SocketTimeoutError``) and the total timeout are K5: the total timer cancels the request wherever
it is and surfaces as a plain ``TimeoutError``, even if it fired during connection setup. homecom_alt
1.8.2 sets only ``ClientTimeout(total=15)``, so with it a hanging connection setup stays K5.
"""
from __future__ import annotations

import enum
from datetime import UTC
from email.utils import parsedate_to_datetime

from aiohttp import (
    ClientConnectionError,
    ClientConnectorError,
    ClientPayloadError,
    ClientResponseError,
    ConnectionTimeoutError,
    ContentTypeError,
)
from homecom_alt import ApiError, AuthFailedError, InvalidSensorDataError, NotRespondingError

K3_MIN_WAIT = 60.0
K3_MAX_WAIT = 3600.0  # an absurd Retry-After must not park the service for longer than the window


class Action(enum.Enum):
    AUTH_REQUIRED = "auth_required"
    RETRY_AFTER = "retry_after"  # K3: after Retry-After, at least 60 s, at most 3 POSTs per hour
    RETRY_BACKOFF = "retry_backoff"  # K4: 30 s doubling up to 15 min, unbounded


class RefreshClass(enum.Enum):
    """Value: (rotation status, action under D11 (b))."""

    K0 = ("unclear or processed: get_token returned without new tokens", Action.AUTH_REQUIRED)
    K1 = ("final: HTTP 400 (or empty JSON body)", Action.AUTH_REQUIRED)
    K2 = ("final: HTTP 401 from the token endpoint", Action.AUTH_REQUIRED)
    K3 = ("not processed (assumption): HTTP 429", Action.RETRY_AFTER)
    K4 = ("provably not sent: connection setup failed or timed out", Action.RETRY_BACKOFF)
    K5 = ("unclear: read or total timeout", Action.AUTH_REQUIRED)
    K6 = ("unclear or processed: aborted after sending", Action.AUTH_REQUIRED)
    K7 = ("unclear: HTTP 500 or other status", Action.AUTH_REQUIRED)
    K8 = ("unclear: HTTP 403/404/502/504", Action.AUTH_REQUIRED)
    K9 = ("processed, probably rotated and lost: unusable 200 body", Action.AUTH_REQUIRED)
    K10 = ("unclear or processed: unexpected exception", Action.AUTH_REQUIRED)

    @property
    def rotation(self) -> str:
        return self.value[0]

    @property
    def action(self) -> Action:
        return self.value[1]


def _status(error: BaseException | None) -> int | None:
    return error.status if isinstance(error, ClientResponseError) else None


def classify(error: Exception) -> RefreshClass:
    """Map an exception raised by ``get_token(force=True)`` of the refresh instance to table K."""
    cause = error.__cause__
    if isinstance(error, AuthFailedError):
        # 400 arrives as None and ends in "Failed to refresh" without a cause, as does an empty
        # JSON body (K9, not distinguishable; both need a login).
        return RefreshClass.K2 if _status(cause) == 401 else RefreshClass.K1
    if isinstance(error, NotRespondingError):
        if _status(cause) == 429:
            return RefreshClass.K3
        # ConnectionTimeoutError is a TimeoutError too; it must be checked first.
        if isinstance(cause, (ClientConnectorError, ConnectionTimeoutError)):
            return RefreshClass.K4
        if isinstance(cause, TimeoutError):  # SocketTimeoutError, total timeout
            return RefreshClass.K5
        return RefreshClass.K10
    if isinstance(error, ApiError):
        return RefreshClass.K7
    if isinstance(error, AttributeError):
        return RefreshClass.K8  # 403/404/502/504 arrive as {} and fail on {}.json()
    if isinstance(error, (InvalidSensorDataError, ContentTypeError, KeyError)):
        return RefreshClass.K9
    if isinstance(error, (ClientConnectorError, ConnectionTimeoutError)):
        return RefreshClass.K4
    if isinstance(error, TimeoutError):
        return RefreshClass.K5  # raw, e.g. while reading the response body
    if isinstance(error, (ClientConnectionError, ClientPayloadError)):
        return RefreshClass.K6  # ServerDisconnectedError, ClientOSError, payload errors
    return RefreshClass.K10


def retry_after(error: Exception, now: float) -> float:
    """Seconds to wait after a 429 (K3): ``Retry-After`` (seconds or HTTP date), 60 s to 1 h."""
    cause = error.__cause__
    headers = cause.headers if isinstance(cause, ClientResponseError) else None
    raw = headers.get("Retry-After") if headers else None
    seconds = 0.0
    if raw:
        try:
            seconds = float(raw)
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                seconds = when.timestamp() - now
    if seconds != seconds:  # NaN
        seconds = 0.0
    return min(max(seconds, K3_MIN_WAIT), K3_MAX_WAIT)
