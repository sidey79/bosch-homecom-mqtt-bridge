"""Unverified ``exp``/``iat`` of an access token, shared by ``login`` and the token manager.

The signature is not verified: the values only decide when to refresh. Implausible values count
as unreadable, so a broken or hostile token can neither crash a caller nor look fresh forever:
an ``exp`` more than ``MAX_EXP_AHEAD`` (30 days) after ``now`` counts as unreadable as well.
"""
from __future__ import annotations

import math
import time

import jwt

MAX_EXP_AHEAD = 30 * 86400


def _number(value: object) -> float | None:
    """A finite JSON number as float; ``bool``, ``inf``, ``nan`` and huge integers are ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def plausible_exp(exp: float, now: float | None = None) -> bool:
    """``0 < exp <= now + MAX_EXP_AHEAD``; ``now`` defaults to the wall clock."""
    return 0 < exp <= (time.time() if now is None else now) + MAX_EXP_AHEAD


def token_times(token: object, now: float | None = None) -> tuple[float, float | None] | None:
    """``(exp, iat)`` of a JWT; ``None`` without a plausible ``exp`` (``plausible_exp``).

    An ``iat`` that is not a number or not before ``exp`` is dropped (``None``); callers then use
    the time the token was obtained instead.
    """
    if not isinstance(token, str) or not token:
        return None
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except (jwt.PyJWTError, ValueError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = _number(claims.get("exp"))
    if exp is None or not plausible_exp(exp, now):
        return None
    iat = _number(claims.get("iat"))
    return exp, (iat if iat is not None and iat < exp else None)


def persistable_exp(token: object, now: float | None = None) -> int | None:
    """``exp`` as the positive integer the store accepts, or ``None`` (e.g. for 0 < exp < 1)."""
    times = token_times(token, now)
    return int(times[0]) if times and times[0] >= 1 else None
