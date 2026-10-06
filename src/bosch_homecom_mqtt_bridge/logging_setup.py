"""Logging configuration with a filter that redacts secrets from every record.

Redacted are: values of known secret keys (tokens, codes, verifier, password) in ``key=value``
and JSON form, anything shaped like a JWT, bearer credentials, the password part of URL
credentials, and explicitly registered secret values. The login URL stays readable: ``state``
and ``code_challenge`` are not secret keys.
"""
from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

MASK = "***"

_SECRET_KEY_VALUE = re.compile(
    r"(?i)\b(refresh_token|access_token|id_token|token|code_verifier|code|password)\b"
    r"""(["']?\s*[:=]\s*["']?)"""
    r"""([^\s"'&,;}]+)"""
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_URL_PASSWORD = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+@", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\b(bearer\s+)\S+")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with all known secret shapes and the given secret values masked."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    text = _JWT.sub(MASK, text)
    text = _BEARER.sub(rf"\g<1>{MASK}", text)
    text = _URL_PASSWORD.sub(rf"\g<1>{MASK}@", text)
    return _SECRET_KEY_VALUE.sub(rf"\g<1>\g<2>{MASK}", text)


class RedactionFilter(logging.Filter):
    """Rewrites message and exception text of each record before any handler formats it."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)
        self._formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self._secrets)
        return True


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
