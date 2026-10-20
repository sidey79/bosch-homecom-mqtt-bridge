import asyncio
import base64
import hashlib
import json
import logging
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from bosch_homecom_mqtt_bridge import mqtt_login as mqtt_login_module
from bosch_homecom_mqtt_bridge.auth.store import AuthStore, AuthUpdate
from bosch_homecom_mqtt_bridge.mqtt_login import LOGIN_FAILED, MqttLogin

from .test_login import RAW_CODE, REFRESH, TOKEN_URL, FakeResponse, FakeSession, ListHandler, fake_access_token, http_error


class FakePublisher:
    def __init__(self) -> None:
        self.handler = None
        self.logins: list[dict] = []
        self.errors: list[tuple[str, str]] = []

    def on_login_command(self, handler) -> None:
        self.handler = handler

    def publish_login(self, state: str, url: str | None = None, expires_at: datetime | None = None) -> None:
        self.logins.append({"state": state, "url": url, "expires_at": expires_at})

    def publish_error(self, code: str, message: str) -> None:
        self.errors.append((code, message))


class MqttLoginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "auth.json"
        self.store = AuthStore(self.path)
        await self.store.locked_update(self._seed)
        self.before = self.path.read_bytes()
        self.access = fake_access_token()
        self.session = FakeSession(self.token_ok, lambda *args: FakeResponse(200, []))
        self.publisher = FakePublisher()
        self.login = MqttLogin(self.store, "bosch", self.session, self.publisher)  # type: ignore[arg-type]
        self.log_handler = ListHandler()
        root = logging.getLogger()
        self._saved_level = root.level
        root.addHandler(self.log_handler)
        root.setLevel(logging.DEBUG)

    async def asyncTearDown(self) -> None:
        await self.login.aclose()
        root = logging.getLogger()
        root.removeHandler(self.log_handler)
        root.setLevel(self._saved_level)
        self._tmp.cleanup()

    @staticmethod
    async def _seed(_current):
        return AuthUpdate("FAKE-previous-refresh-token", "bosch")

    def token_ok(self, method, url, kwargs):
        return FakeResponse(200, {"access_token": self.access, "refresh_token": REFRESH, "expires_in": 3600})

    def send(self, text: str, retained: bool = False) -> None:
        assert self.publisher.handler is not None
        self.publisher.handler(text.encode("utf-8"), retained)

    def started(self) -> tuple[str, dict[str, list[str]]]:
        """Start a login; return its URL and query."""
        self.login.start()
        self.send("start")
        pending = self.publisher.logins[-1]
        self.assertEqual(pending["state"], "pending")
        return pending["url"], parse_qs(urlsplit(pending["url"]).query)

    def console_line(self, state: str) -> str:
        url = f"com.bosch.tt.dashtt.pointt://app/login?code={quote(RAW_CODE, safe='')}&state={state}"
        return f"Failed to launch '{url}' because the scheme does not have a registered handler."

    async def finished(self) -> None:
        task = self.login._task
        if task is not None:
            await task

    def assert_no_secrets(self) -> None:
        secrets = [RAW_CODE, quote(RAW_CODE, safe=""), self.access, REFRESH]
        for _, url, kwargs in self.session.calls:
            if url == TOKEN_URL:
                secrets.append(unquote(kwargs["data"].split("code_verifier=")[1]))
        published = json.dumps([self.publisher.errors, [{**m, "expires_at": None} for m in self.publisher.logins]])
        combined = "\n".join([*self.log_handler.messages, published])
        for secret in secrets:
            self.assertNotIn(secret, combined)

    async def test_start_clears_an_old_login_url(self) -> None:
        self.login.start()
        self.assertEqual(self.publisher.logins, [{"state": "idle", "url": None, "expires_at": None}])
        self.assertIsNotNone(self.publisher.handler)

    async def test_console_line_completes_the_login(self) -> None:
        url, query = self.started()
        self.assertTrue(url.startswith("https://singlekey-id.com/auth/connect/authorize?"))
        expires_at = self.publisher.logins[-1]["expires_at"]
        self.assertLess(abs(expires_at - (datetime.now(UTC) + timedelta(seconds=600))), timedelta(seconds=5))

        self.send(self.console_line(query["state"][0]))
        await self.finished()

        self.assertEqual(self.publisher.errors, [])
        self.assertEqual(self.publisher.logins[-1]["state"], "done")
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["generation"], saved["refresh_token"], saved["access_token"]), (2, REFRESH, self.access))
        self.assertIsNotNone(saved["login_id"])  # a new login the token manager adopts
        body = self.session.token_body()
        self.assertIn(f"code={quote(RAW_CODE, safe='')}&", body)
        verifier = unquote(body.split("code_verifier=")[1])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(self.session.urls(), [TOKEN_URL])  # no gateway query, the poller discovers
        self.assert_no_secrets()

    async def test_code_without_pending_login_is_rejected(self) -> None:
        self.login.start()
        self.send(self.console_line("S1"))
        self.assertEqual(self.publisher.errors, [(LOGIN_FAILED, "no login pending; send start first")])
        self.assertEqual(self.session.calls, [])

    async def test_foreign_state_keeps_the_login_pending(self) -> None:
        _, query = self.started()
        self.send(self.console_line("foreign-state"))
        self.send("no code in here")
        self.assertEqual([code for code, _ in self.publisher.errors], [LOGIN_FAILED, LOGIN_FAILED])
        self.assertEqual(self.session.calls, [])
        self.send(self.console_line(query["state"][0]))
        await self.finished()
        self.assertEqual(self.publisher.logins[-1]["state"], "done")

    async def test_retained_and_empty_commands_are_ignored(self) -> None:
        self.login.start()
        self.send("start", retained=True)
        self.send("   ")
        self.assertEqual([m["state"] for m in self.publisher.logins], ["idle"])
        self.assertEqual(self.publisher.errors, [])

    async def test_rejected_code_ends_the_login_and_keeps_the_file(self) -> None:
        def token_400(method, url, kwargs):
            raise http_error(method, url, 400)

        self.session.handlers[TOKEN_URL] = token_400
        _, query = self.started()
        self.send(self.console_line(query["state"][0]))
        await self.finished()
        self.assertEqual(self.publisher.logins[-1]["state"], "idle")
        self.assertEqual(self.publisher.errors[-1][0], LOGIN_FAILED)
        self.assertIn("rejected", self.publisher.errors[-1][1])
        self.assertEqual(self.path.read_bytes(), self.before)
        # The code may be spent: a second input needs a new start.
        self.send(self.console_line(query["state"][0]))
        self.assertEqual(self.publisher.errors[-1][1], "no login pending; send start first")
        self.assertEqual(self.session.urls(), [TOKEN_URL])
        self.assert_no_secrets()

    async def test_a_new_start_replaces_the_pending_login(self) -> None:
        _, first = self.started()
        self.send("start")
        second = parse_qs(urlsplit(self.publisher.logins[-1]["url"]).query)
        self.assertNotEqual(first["state"], second["state"])
        self.send(self.console_line(first["state"][0]))
        self.assertEqual(self.publisher.errors[-1][1], "the input holds no code for the pending login")

    async def test_pending_login_expires(self) -> None:
        self.login = MqttLogin(self.store, "bosch", self.session, self.publisher, lifetime=0.05)  # type: ignore[arg-type]
        _, query = self.started()
        await asyncio.sleep(0.1)
        self.assertEqual(self.publisher.logins[-1]["state"], "idle")
        self.send(self.console_line(query["state"][0]))
        self.assertEqual(self.publisher.errors[-1][1], "no login pending; send start first")

    async def test_commands_while_redeeming_are_refused(self) -> None:
        release = asyncio.Event()

        async def slow_token(method, url, kwargs):
            await release.wait()
            return self.token_ok(method, url, kwargs)

        async def request(method, url, **kwargs):
            self.session.calls.append((method, url, kwargs))
            return await slow_token(method, url, kwargs)

        self.session.request = request  # type: ignore[method-assign]
        _, query = self.started()
        self.send(self.console_line(query["state"][0]))
        await asyncio.sleep(0.01)
        self.send("start")
        self.assertEqual(self.publisher.errors, [(LOGIN_FAILED, "a code is being redeemed; try again later")])
        release.set()
        await self.login.aclose()  # waits for the running exchange and its write
        self.assertEqual(self.publisher.logins[-1]["state"], "done")
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], REFRESH)

    async def test_oversized_payload_is_refused_unparsed(self) -> None:
        self.started()
        self.send("a" * (mqtt_login_module.MAX_PAYLOAD + 1))
        self.assertEqual(self.publisher.errors, [(LOGIN_FAILED, "the command is longer than 4096 bytes")])
        self.assertEqual(self.publisher.logins[-1]["state"], "pending")

    async def test_closing_ends_the_pending_login_and_ignores_later_commands(self) -> None:
        _, query = self.started()
        await self.login.aclose()
        self.assertEqual(self.publisher.logins[-1]["state"], "idle")
        self.send("start")
        self.send(self.console_line(query["state"][0]))
        self.assertEqual(self.publisher.logins[-1]["state"], "idle")
        self.assertEqual((self.publisher.errors, self.session.calls), ([], []))

    async def test_the_url_with_the_code_wins_in_a_console_line(self) -> None:
        _, query = self.started()
        line = f"login?x=1 https://example.invalid/app.js?v=2 {self.console_line(query['state'][0])}"
        self.send(line)
        await self.finished()
        self.assertEqual(self.publisher.logins[-1]["state"], "done")

    async def test_unexpected_failure_is_reported_by_type(self) -> None:
        def broken(method, url, kwargs):
            raise KeyError("FAKE-detail")

        self.session.handlers[TOKEN_URL] = broken
        _, query = self.started()
        self.send(self.console_line(query["state"][0]))
        await self.finished()
        self.assertEqual(self.publisher.errors[-1], (LOGIN_FAILED, "login failed (KeyError)"))
        self.assertEqual(self.publisher.logins[-1]["state"], "idle")


class LifetimeTest(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(mqtt_login_module.LOGIN_LIFETIME, 600.0)
        self.assertLessEqual(mqtt_login_module.CLOSE_TIMEOUT, 15.0)


if __name__ == "__main__":
    unittest.main()
