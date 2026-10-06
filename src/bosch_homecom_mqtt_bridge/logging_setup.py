"""Logging configuration with a filter that redacts secrets from every record.

Redacted are: values of secret keys in ``key=value``, ``key: value`` and JSON form, also in
escaped and doubly escaped JSON (any key ending in token, secret, password, passwd or pwd, also with prefix, in
camelCase or with a hash suffix such as ``mqtt_password``, ``refreshToken`` or ``passwordHash``,
plus api key, code, auth_code, code_verifier and verifier; quoted values completely, also when the
closing quote was cut off), cookie headers (quoted, as a JSON list or up to the end of the
line), anything shaped like a JWT, bearer credentials,
``Authorization`` header values, the password part of URL credentials, and explicitly registered
secret values (also at runtime via ``add_secret``). The login URL stays readable: ``state`` and
``code_challenge`` are not secret keys, and ``status code: 400`` is not a secret either.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Iterable

MASK = "***"
# Runtime secrets (tokens, codes) are bounded: short values would mask ordinary words, and a
# service that rotates tokens for months must not grow the list without limit.
MIN_RUNTIME_SECRET_LENGTH = 8
MAX_RUNTIME_SECRETS = 32

_SECRET_KEY = (
    r"(?:[\w-]*?(?:token|secret|(?:password|passwd|pwd)(?:[_-]?hash)?|api[_-]?key)"
    r"|(?:auth(?:orization)?[_-]?)?code(?:[_-]?verifier)?"
    r"|verifier)"
)
# Quoted values: an escape is a backslash plus any character, a line break or the end of the text.
_QUOTED = r""""(?:[^"\\]|\\(?:.|\n|$))*(?:"|$)|'(?:[^'\\]|\\(?:.|\n|$))*(?:'|$)"""
# Escaped (also doubly escaped) JSON strings end at the next backslash-escaped quote. The
# possessive "\\++" never backtracks through a run of backslashes.
_ESCAPED_QUOTED = r"""\\++"(?:[^"\\]|\\++[^"\\]|\\++$)*(?:\\++"|$)"""
_SECRET_KEY_VALUE = re.compile(
    rf"(?i)(?<![\w-])(?<!status )({_SECRET_KEY})(?![\w-])"
    # One or more backslashes before the quote: escaped and doubly escaped JSON.
    r"""(\\*+["']?\s*[:=]\s*)"""
    # Escaped JSON first, then quoted values (closing quote optional: a value cut off by a length
    # limit is masked up to the end; a trailing "\" or one before a line break is part of the
    # value), then bare values. Bare values end at ";" only for cookies.
    rf"""({_ESCAPED_QUOTED}|{_QUOTED}|[^\s"'&,}}]+)"""
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
# The password ends at the last "@" of the authority (RFC 3986: before "/", "?" or "#"), so a raw
# "@" inside it does not leak a part, and "@" in a path or query is left alone. The lookbehind
# anchors the scheme; together with the bounded repeats the match stays linear.
_URL_PASSWORD = re.compile(
    r"((?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://[^:/?#\s@]{0,256}:)[^\s/?#]{1,1024}@", re.IGNORECASE
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)\S+")
_AUTHORIZATION = re.compile(
    r"""(?i)\b(authorization["']?\s*[:=]\s*["']?(?:(?:basic|bearer|digest)\s+)?)([^\s"',;}]+)"""
)
# Cookie and Set-Cookie: a quoted value up to its closing quote, a JSON list up to "]", anything
# else up to the end of the line.
_COOKIE = re.compile(
    r"""(?i)(?<![\w-])((?:set-)?cookie\\*+["']?\s*[:=]\s*)"""
    rf"""({_ESCAPED_QUOTED}|{_QUOTED}|\[(?:{_QUOTED}|[^\]"'])*(?:\]|$)|[^\r\n]+)"""
)


def _mask(value: str) -> str:
    """Mask ``value`` but keep its opening and closing quotes or brackets."""
    opener = re.match(r"""\\*["']|\[""", value)
    if not opener:
        return MASK
    start = opener.group()
    end = "]" if start == "[" else start
    closed = len(value) > len(start) and value.endswith(end)
    return f"{start}{MASK}{end if closed else ''}"


def _mask_value(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{_mask(match.group(3))}"


def _mask_cookie(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_mask(match.group(2))}"


def _redact_sorted(text: str, secrets: tuple[str, ...]) -> str:
    """``redact`` for secrets that are already non-empty and sorted longest first."""
    for secret in secrets:
        text = text.replace(secret, MASK)
    text = _JWT.sub(MASK, text)
    text = _AUTHORIZATION.sub(rf"\g<1>{MASK}", text)
    text = _BEARER.sub(rf"\g<1>{MASK}", text)
    text = _COOKIE.sub(_mask_cookie, text)
    text = _URL_PASSWORD.sub(rf"\g<1>{MASK}@", text)
    return _SECRET_KEY_VALUE.sub(_mask_value, text)


def _longest_first(secrets: Iterable[str]) -> tuple[str, ...]:
    # Longest first, so that a secret containing another one is masked as a whole.
    return tuple(sorted({s for s in secrets if isinstance(s, str) and s}, key=lambda s: (-len(s), s)))


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with all known secret shapes and the given secret values masked."""
    return _redact_sorted(text, _longest_first(secrets))


class RedactionFilter(logging.Filter):
    """Rewrites message and exception text of each record before any handler formats it.

    Secrets passed to the constructor (e.g. ``MQTT_PASSWORD``) stay for the filter's lifetime.
    Secrets added at runtime are kept only if they are strings of at least
    ``MIN_RUNTIME_SECRET_LENGTH`` characters, and only the newest ``MAX_RUNTIME_SECRETS``.
    ``add_secret`` may be called from any thread: it replaces an immutable snapshot under a lock,
    and ``filter`` reads that snapshot once per record without locking.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._fixed = _longest_first(secrets)
        self._runtime: tuple[str, ...] = ()  # oldest first
        self._lock = threading.Lock()
        self._snapshot = self._fixed
        self._formatter = logging.Formatter()

    def add_secret(self, secret: object) -> None:
        """Register a secret value that only becomes known at runtime (e.g. a token)."""
        if not isinstance(secret, str) or len(secret) < MIN_RUNTIME_SECRET_LENGTH:
            return
        with self._lock:
            runtime = (*(s for s in self._runtime if s != secret), secret)[-MAX_RUNTIME_SECRETS:]
            self._runtime = runtime
            self._snapshot = _longest_first((*self._fixed, *runtime))

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = self._snapshot
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must neither crash nor bypass redaction
            message = f"{record.msg} [unformattable log arguments]"
        record.msg = _redact_sorted(message, secrets)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _redact_sorted(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = _redact_sorted(record.stack_info, secrets)
        return True


def add_secret(secret: object) -> None:
    """Register ``secret`` with every ``RedactionFilter`` on the root logger's handlers."""
    for handler in logging.getLogger().handlers:
        for candidate in handler.filters:
            if isinstance(candidate, RedactionFilter):
                candidate.add_secret(secret)


def setup_logging(level: str, secrets: Iterable[str] = ()) -> logging.Handler:
    """Install a single stderr handler with redaction on the root logger and return it."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactionFilter(secrets))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    return handler
