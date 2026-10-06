import asyncio
import base64
import contextlib
import errno
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, quote, unquote, urlsplit

import jwt
from aiohttp import ClientResponseError, RequestInfo
from homecom_alt import HomeComAlt
from homecom_alt.const import BOSCHCOM_DOMAIN, BOSCHCOM_ENDPOINT_GATEWAYS, OAUTH_BROWSER_VERIFIER
from multidict import CIMultiDict, CIMultiDictProxy
from tenacity import wait_none
from yarl import URL

from bosch_homecom_mqtt_bridge import __main__ as cli
from bosch_homecom_mqtt_bridge.auth import login as login_module
from bosch_homecom_mqtt_bridge.auth import store as store_module
from bosch_homecom_mqtt_bridge.auth.login import extract_code, run_login
from bosch_homecom_mqtt_bridge.auth.store import AuthStore, AuthUpdate
from bosch_homecom_mqtt_bridge.logging_setup import RedactionFilter

ROOT = Path(__file__).resolve().parents[1]
# The real URLs: the library's 400 -> None path depends on the exact token URL string.
TOKEN_URL = "https://singlekey-id.com/auth/connect/token"
GATEWAYS_URL = BOSCHCOM_DOMAIN + BOSCHCOM_ENDPOINT_GATEWAYS
REFRESH = "FAKE-refresh-token-0123456789"
RAW_CODE = "FAKE-code/with+special=chars"
JWT_KEY = "test-signing-key-for-fake-jwts-only-0123456789"
LIFETIME = 3600


def fake_access_token() -> str:
    now = int(time.time())
    return jwt.encode({"iat": now, "exp": now + LIFETIME}, JWT_KEY, algorithm="HS256")


class FakeResponse:
    def __init__(self, status: int, payload: object, error: BaseException | None = None) -> None:
        self.status = status
        self._payload = payload
        self._error = error

    async def json(self) -> object:
        if self._error is not None:
            raise self._error
        return self._payload


def http_error(method: str, url: str, status: int) -> ClientResponseError:
    info = RequestInfo(URL(url), method.upper(), CIMultiDictProxy(CIMultiDict()), URL(url))
    return ClientResponseError(info, (), status=status)


class FakeSession:
    """Stands in for aiohttp.ClientSession; handlers return a FakeResponse or raise."""

    def __init__(self, token_handler, gateway_handler) -> None:
        self.handlers = {TOKEN_URL: token_handler, GATEWAYS_URL: gateway_handler}
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.handlers[url](method, url, kwargs)

    def urls(self) -> list[str]:
        return [url for _, url, _ in self.calls]

    def token_body(self) -> str:
        bodies = [kwargs["data"] for _, url, kwargs in self.calls if url == TOKEN_URL]
        assert len(bodies) == 1, bodies
        return bodies[0]


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


class LoginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "auth.json"
        self.store = AuthStore(self.path)
        self.access = fake_access_token()
        self.log_handler = ListHandler()
        root = logging.getLogger()
        self._saved_level = root.level
        root.addHandler(self.log_handler)
        root.setLevel(logging.DEBUG)
        # Seed generation n = 4.
        for i in range(4):
            asyncio.run(self.store.locked_update(self._update(f"FAKE-previous-{i}")))
        self.before = self.path.read_bytes()

    def tearDown(self) -> None:
        root = logging.getLogger()
        root.removeHandler(self.log_handler)
        root.setLevel(self._saved_level)
        self._tmp.cleanup()

    @staticmethod
    def _update(token: str):
        async def fn(_current):
            return AuthUpdate(token, "bosch")

        return fn

    def token_ok(self, method, url, kwargs):
        return FakeResponse(200, {"access_token": self.access, "refresh_token": REFRESH, "expires_in": LIFETIME})

    @staticmethod
    def gateways_ok(method, url, kwargs):
        return FakeResponse(200, [{"deviceId": "101506113", "deviceType": "wddw2"}])

    def login(self, session: FakeSession, answer, brand: str = "bosch", store: AuthStore | None = None):
        out, err = io.StringIO(), io.StringIO()

        def read_input(_prompt: str) -> str:
            url = next(line for line in out.getvalue().splitlines() if line.startswith("https://"))
            return answer(parse_qs(urlsplit(url).query)["state"][0])

        exit_code = asyncio.run(
            run_login(store or self.store, brand, session, read_input=read_input, out=out, err=err)
        )
        return exit_code, out.getvalue(), err.getvalue()

    def assert_no_secrets(self, session: FakeSession, *texts: str) -> None:
        secrets = [RAW_CODE, quote(RAW_CODE, safe=""), self.access, REFRESH]
        token_bodies = [kwargs["data"] for _, url, kwargs in session.calls if url == TOKEN_URL]
        for body in token_bodies:
            secrets.append(unquote(body.split("code_verifier=")[1]))
        combined = "\n".join([*texts, *self.log_handler.messages])
        for secret in secrets:
            self.assertNotIn(secret, combined)

    def redirect(self, state: str) -> str:
        return f"com.bosch.tt.dashtt.pointt://app/login?code={quote(RAW_CODE, safe='')}&state={state}"

    def test_valid_code_writes_next_generation_and_lists_gateways(self) -> None:
        session = FakeSession(self.token_ok, self.gateways_ok)
        exit_code, out, err = self.login(session, self.redirect)

        self.assertEqual(exit_code, 0, err)
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["generation"], saved["refresh_token"], saved["brand"]), (5, REFRESH, "bosch"))
        self.assertIsNone(saved["last_refresh_at"])  # m9: only the service refresh sets it
        # D7: the access token and its exp are persisted.
        self.assertEqual(saved["access_token"], self.access)
        self.assertEqual(saved["exp"], jwt.decode(self.access, options={"verify_signature": False})["exp"])
        self.assertIn("Generation 5", out)
        self.assertIn("Gateway 101506113: Typ wddw2", out)
        remaining = int(re.search(r"gültig noch (-?\d+) s", out).group(1))
        self.assertTrue(LIFETIME - 60 <= remaining <= LIFETIME, remaining)

        body = session.token_body()
        self.assertIn(f"code={quote(RAW_CODE, safe='')}&", body)
        self.assertIn("grant_type=authorization_code", body)
        self.assertIn(quote("com.bosch.tt.dashtt.pointt://app/login", safe=""), body)
        self.assertNotIn(OAUTH_BROWSER_VERIFIER, body)
        # Exactly one token request (the exchange), then the gateway list with the new bearer.
        self.assertEqual(session.urls(), [TOKEN_URL, GATEWAYS_URL])
        self.assertEqual(session.calls[1][2]["headers"]["Authorization"], f"Bearer {self.access}")
        self.assert_no_secrets(session, out, err)

    def test_login_url_matches_verifier_and_brand(self) -> None:
        session = FakeSession(self.token_ok, self.gateways_ok)
        exit_code, out, _ = self.login(session, self.redirect)
        self.assertEqual(exit_code, 0)
        url = next(line for line in out.splitlines() if line.startswith("https://"))
        query = parse_qs(urlsplit(url).query)
        verifier = unquote(session.token_body().split("code_verifier=")[1])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], ["com.bosch.tt.dashtt.pointt://app/login"])
        self.assertTrue(url.startswith("https://singlekey-id.com/auth/connect/authorize?"))

    def test_buderus_uses_buderus_parameters(self) -> None:
        session = FakeSession(self.token_ok, self.gateways_ok)
        exit_code, out, err = self.login(session, lambda state: RAW_CODE, brand="buderus")
        self.assertEqual(exit_code, 0, err)
        url = next(line for line in out.splitlines() if line.startswith("https://"))
        self.assertEqual(parse_qs(urlsplit(url).query)["redirect_uri"], ["com.buderus.tt.dashtt://app/login"])
        self.assertIn(quote("com.buderus.tt.dashtt://app/login", safe=""), session.token_body())
        self.assertEqual(json.loads(self.path.read_text())["brand"], "buderus")

    def test_bare_code_is_url_encoded(self) -> None:
        for answer in (RAW_CODE, quote(RAW_CODE, safe="")):
            with self.subTest(answer=answer):
                session = FakeSession(self.token_ok, self.gateways_ok)
                exit_code, _, err = self.login(session, lambda state, a=answer: f"  {a}\n")
                self.assertEqual(exit_code, 0, err)
                self.assertIn(f"code={quote(RAW_CODE, safe='')}&", session.token_body())

    def test_http_400_reports_and_keeps_file(self) -> None:
        def token_400(method, url, kwargs):
            raise http_error(method, url, 400)

        session = FakeSession(token_400, self.gateways_ok)
        exit_code, out, err = self.login(session, self.redirect)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Code ungültig", err)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(session.urls(), [TOKEN_URL])
        self.assert_no_secrets(session, out, err)

    def test_other_exchange_failures_keep_file(self) -> None:
        def raising(exc):
            def handler(method, url, kwargs):
                raise exc

            return handler

        cases = {
            "401": raising(http_error("post", TOKEN_URL, 401)),
            "502": raising(http_error("post", TOKEN_URL, 502)),
            "500": raising(http_error("post", TOKEN_URL, 500)),
            "timeout": raising(TimeoutError()),
            "empty": lambda method, url, kwargs: FakeResponse(200, {}),
        }
        for name, handler in cases.items():
            with self.subTest(name):
                session = FakeSession(handler, self.gateways_ok)
                exit_code, out, err = self.login(session, self.redirect)
                self.assertNotEqual(exit_code, 0)
                self.assertTrue(err.strip())
                self.assertEqual(self.path.read_bytes(), self.before)
                self.assertNotIn(GATEWAYS_URL, session.urls())
                self.assert_no_secrets(session, out, err)

    def test_timeout_while_reading_token_body_is_reported(self) -> None:
        session = FakeSession(lambda method, url, kwargs: FakeResponse(200, None, TimeoutError()), self.gateways_ok)
        exit_code, out, err = self.login(session, self.redirect)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Token-Austausch fehlgeschlagen (TimeoutError)", err)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_secrets(session, out, err)

    def test_write_failure_after_exchange_says_token_was_not_saved(self) -> None:
        session = FakeSession(self.token_ok, self.gateways_ok)
        with mock.patch.object(store_module.os, "replace", side_effect=OSError(errno.ENOSPC, "FAKE-strerror")):
            exit_code, out, err = self.login(session, self.redirect)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Token ausgestellt, aber nicht gespeichert", err)
        self.assertIn("ENOSPC", err)
        self.assertNotIn("FAKE-strerror", err)
        self.assertNotIn("Login gespeichert", out)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(session.urls(), [TOKEN_URL])
        self.assert_no_secrets(session, out, err)

    def test_storage_error_before_exchange_does_not_redeem_code(self) -> None:
        target = self.path.with_name("real.json")
        self.path.rename(target)
        self.path.symlink_to(target)
        session = FakeSession(self.token_ok, self.gateways_ok)
        exit_code, _, err = self.login(session, self.redirect)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("ELOOP", err)
        self.assertIn("nicht eingelöst", err)
        self.assertEqual(session.calls, [])

    def test_secrets_are_registered_for_redaction(self) -> None:
        redaction = RedactionFilter()
        self.log_handler.addFilter(redaction)
        session = FakeSession(self.token_ok, self.gateways_ok)
        exit_code, _, err = self.login(session, self.redirect)
        self.assertEqual(exit_code, 0, err)
        verifier = unquote(session.token_body().split("code_verifier=")[1])
        logging.getLogger("test.login").info("%s|%s|%s|%s", verifier, RAW_CODE, self.access, REFRESH)
        logged = self.log_handler.messages[-1]
        for secret in (verifier, RAW_CODE, self.access, REFRESH):
            self.assertNotIn(secret, logged)

    def test_wrong_or_missing_state_sends_no_token_request(self) -> None:
        answers = [
            lambda state: self.redirect("someone-elses-state"),
            lambda state: f"com.bosch.tt.dashtt.pointt://app/login?code={RAW_CODE}",
        ]
        for n, answer in enumerate(answers):
            with self.subTest(n):
                session = FakeSession(self.token_ok, self.gateways_ok)
                exit_code, out, err = self.login(session, answer)
                self.assertNotEqual(exit_code, 0)
                self.assertIn("state", err)
                self.assertEqual(session.calls, [])
                self.assertEqual(self.path.read_bytes(), self.before)
                self.assert_no_secrets(session, out, err)

    def test_aborted_input_sends_no_request(self) -> None:
        def eof(_state: str) -> str:
            raise EOFError

        session = FakeSession(self.token_ok, self.gateways_ok)
        exit_code, _, err = self.login(session, eof)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("abgebrochen", err)
        self.assertEqual(session.calls, [])

    def test_gateway_timeout_keeps_saved_login(self) -> None:
        def gateway_timeout(method, url, kwargs):
            raise TimeoutError

        session = FakeSession(self.token_ok, gateway_timeout)
        with mock.patch.object(HomeComAlt.async_get_devices.retry, "wait", wait_none()):
            exit_code, out, err = self.login(session, self.redirect)

        self.assertNotEqual(exit_code, 0)
        self.assertIn("Prüfe Gateways", out)
        self.assertIn("Login gespeichert, Gateway-Abfrage fehlgeschlagen (NotRespondingError)", err)
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["generation"], saved["refresh_token"]), (5, REFRESH))
        # tenacity: five attempts in total.
        self.assertEqual(session.urls(), [TOKEN_URL] + [GATEWAYS_URL] * 5)
        self.assert_no_secrets(session, out, err)

    def test_gateway_http_error_keeps_saved_login(self) -> None:
        def gateway_403(method, url, kwargs):
            raise http_error(method, url, 403)

        session = FakeSession(self.token_ok, gateway_403)
        exit_code, _, err = self.login(session, self.redirect)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Login gespeichert, Gateway-Abfrage fehlgeschlagen (AttributeError)", err)
        self.assertEqual(json.loads(self.path.read_text())["generation"], 5)

    def test_broken_gateway_json_keeps_saved_login(self) -> None:
        broken = json.JSONDecodeError("Expecting value", "FAKE-body", 0)
        session = FakeSession(self.token_ok, lambda method, url, kwargs: FakeResponse(200, None, broken))
        exit_code, out, err = self.login(session, self.redirect)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Login gespeichert, Gateway-Abfrage fehlgeschlagen (JSONDecodeError)", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(self.path.read_text())["generation"], 5)
        self.assert_no_secrets(session, out, err)

    def test_unexpected_gateway_error_keeps_saved_login(self) -> None:
        def gateway_crash(method, url, kwargs):
            raise RuntimeError("FAKE-detail")

        session = FakeSession(self.token_ok, gateway_crash)
        exit_code, _, err = self.login(session, self.redirect)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("Login gespeichert, Gateway-Abfrage fehlgeschlagen (RuntimeError)", err)
        self.assertNotIn("FAKE-detail", err)
        self.assertEqual(json.loads(self.path.read_text())["generation"], 5)

    def test_cancelled_gateway_query_reports_and_reraises(self) -> None:
        def gateway_cancelled(method, url, kwargs):
            raise asyncio.CancelledError

        session = FakeSession(self.token_ok, gateway_cancelled)
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run_login(self.store, "bosch", session, read_input=lambda _p: RAW_CODE, out=out, err=err))
        self.assertIn("Login gespeichert, Gateway-Abfrage abgebrochen.", err.getvalue())
        self.assertEqual(json.loads(self.path.read_text())["generation"], 5)
        self.assertEqual(session.urls(), [TOKEN_URL, GATEWAYS_URL])
        self.assert_no_secrets(session, out.getvalue(), err.getvalue())

    def test_server_strings_are_not_printed_raw(self) -> None:
        def gateways_odd(method, url, kwargs):
            return FakeResponse(200, [{"deviceId": "evil\x1b[2Jid", "deviceType": None}])

        session = FakeSession(self.token_ok, gateways_odd)
        exit_code, out, err = self.login(session, self.redirect)
        self.assertEqual(exit_code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertIn("Gateway 'evil\\x1b[2Jid': Typ None", out)

    def test_invalid_auth_file_is_reported_without_redeeming_code(self) -> None:
        for name, corrupt in {
            "not json": lambda: self.path.write_text("not json FAKE-content"),
            "directory": lambda: (self.path.unlink(), self.path.mkdir()),
        }.items():
            with self.subTest(name):
                corrupt()
                session = FakeSession(self.token_ok, self.gateways_ok)
                exit_code, out, err = self.login(session, self.redirect)
                self.assertNotEqual(exit_code, 0)
                self.assertEqual(
                    err.strip(),
                    f"{self.path} ist keine gültige Auth-Datei. Datei sichern und entfernen, dann den "
                    "Login erneut starten. Der Code wurde nicht eingelöst.",
                )
                self.assertEqual(session.calls, [])
                self.assert_no_secrets(session, out, err)

    def test_short_code_is_not_registered_for_redaction(self) -> None:
        redaction = RedactionFilter()
        self.log_handler.addFilter(redaction)
        session = FakeSession(self.token_ok, self.gateways_ok)
        exit_code, _, err = self.login(session, lambda state: "abc")
        self.assertEqual(exit_code, 0, err)
        logging.getLogger("test.login").info("abc stays readable")
        self.assertEqual(self.log_handler.messages[-1], "abc stays readable")

    def test_lock_timeout_does_not_redeem_code(self) -> None:
        store = AuthStore(self.path, lock_timeout=0.3)
        fd = os.open(store.lock_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            session = FakeSession(self.token_ok, self.gateways_ok)
            exit_code, _, err = self.login(session, self.redirect, store=store)
        finally:
            os.close(fd)
        self.assertNotEqual(exit_code, 0)
        self.assertIn("nicht eingelöst", err)
        self.assertEqual(session.calls, [])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_missing_directory_is_reported_before_login_url(self) -> None:
        store = AuthStore(Path(self._tmp.name) / "missing" / "auth.json")
        session = FakeSession(self.token_ok, self.gateways_ok)
        out, err = io.StringIO(), io.StringIO()
        exit_code = asyncio.run(
            run_login(store, "bosch", session, read_input=lambda _p: RAW_CODE, out=out, err=err)
        )
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(session.calls, [])


    def test_login_sets_a_new_login_id_and_clears_refresh_marks(self) -> None:
        async def blocked(current):
            return store_module.RefreshMarks("K2", 123.0, (100.0,))

        asyncio.run(self.store.locked_update(blocked))
        ids = []
        for _ in range(2):
            session = FakeSession(self.token_ok, self.gateways_ok)
            exit_code, _out, err = self.login(session, self.redirect)
            self.assertEqual(exit_code, 0, err)
            saved = json.loads(self.path.read_text())
            self.assertRegex(saved["login_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(
                [saved[key] for key in ("refresh_blocked", "blocked_generation", "not_before", "refresh_posts")],
                [None, None, None, []],
            )
            ids.append(saved["login_id"])
        self.assertNotEqual(ids[0], ids[1])

    def test_implausible_exp_is_saved_as_unknown_instead_of_failing(self) -> None:
        # M-2: token_expires_at() of the library would raise on these after the code was redeemed.
        for exp, readable in ((0.5, True), (10**400, False), (True, False), ("soon", False)):
            with self.subTest(exp=exp):
                self.access = jwt.encode({"exp": exp}, JWT_KEY, algorithm="HS256")
                session = FakeSession(self.token_ok, self.gateways_ok)
                exit_code, out, err = self.login(session, self.redirect)
                self.assertEqual(exit_code, 0, err)
                saved = json.loads(self.path.read_text())
                self.assertEqual((saved["access_token"], saved["exp"]), (self.access, None))
                self.assertEqual("Restlaufzeit des Access-Tokens unbekannt" in out, not readable)


class ExtractCodeTest(unittest.TestCase):
    def test_plus_in_redirect_url_survives(self) -> None:
        self.assertEqual(extract_code("app://x/login?code=a+b%2Fc&state=s", "s"), "a%2Bb%2Fc")

    def test_query_without_scheme(self) -> None:
        self.assertEqual(extract_code("?code=abc&state=s", "s"), "abc")

    def test_duplicate_or_empty_code_is_rejected(self) -> None:
        from bosch_homecom_mqtt_bridge.auth.login import LoginError

        for text in ("app://x?code=a&code=b&state=s", "app://x?code=&state=s", "   "):
            with self.subTest(text=text), self.assertRaises(LoginError):
                extract_code(text, "s")


class CliTest(unittest.TestCase):
    def run_cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "bosch_homecom_mqtt_bridge", *args],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src"), **(env or {})},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_help_lists_login_and_run(self) -> None:
        result = self.run_cli("--help")  # without a command the bridge starts (run is the default)
        self.assertEqual(result.returncode, 0)
        self.assertIn("login", result.stdout)
        self.assertIn("run", result.stdout)

    def test_login_rejects_invalid_config(self) -> None:
        result = self.run_cli("login", env={"BOSCH_BRAND": "foo"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("BOSCH_BRAND", result.stderr)

    def run_main_with_login(self, error: BaseException) -> tuple[int, str]:
        async def failing_login(_config) -> int:
            raise error

        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        stderr = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"BOSCH_AUTH_PATH": str(Path(tmp) / "auth.json")}, clear=True
            ), mock.patch.object(login_module, "login", failing_login), contextlib.redirect_stderr(stderr):
                exit_code = cli.main(["login"])
        finally:
            for current in list(root.handlers):
                root.removeHandler(current)
            for previous in saved_handlers:
                root.addHandler(previous)
            root.setLevel(saved_level)
        return exit_code, stderr.getvalue()

    def test_unexpected_error_prints_type_only(self) -> None:
        exit_code, stderr = self.run_main_with_login(RuntimeError("FAKE-secret-detail"))
        self.assertEqual(exit_code, 1)
        self.assertIn("RuntimeError", stderr)
        self.assertNotIn("FAKE-secret-detail", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_keyboard_interrupt_is_reported_as_aborted(self) -> None:
        exit_code, stderr = self.run_main_with_login(KeyboardInterrupt())
        self.assertEqual(exit_code, 1)
        self.assertIn("abgebrochen", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
