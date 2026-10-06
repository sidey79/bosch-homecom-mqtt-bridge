import io
import logging
import unittest

from bosch_homecom_mqtt_bridge.logging_setup import MASK, RedactionFilter, redact, setup_logging

# Built at runtime so no JWT-shaped literal is checked in.
FAKE_JWT = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJleHAiOjF9", "c2lnbmF0dXJl"])
REFRESH = "FAKE-refresh-0123456789abcdef"
VERIFIER = "FAKE-verifier-0123456789abcdef"
CODE = "FAKE-code-0123456789"


class RedactTest(unittest.TestCase):
    def test_key_value_forms(self) -> None:
        samples = [
            f"refresh_token={REFRESH}",
            f'{{"refresh_token": "{REFRESH}"}}',
            f"refresh_token='{REFRESH}'",
            f"code_verifier={VERIFIER}&grant_type=x",
            f"com.bosch.tt.dashtt.pointt://app/login?code={CODE}&state=abc",
            f"password: {REFRESH}",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                result = redact(sample)
                for secret in (REFRESH, VERIFIER, CODE):
                    self.assertNotIn(secret, result)
                self.assertIn(MASK, result)

    def test_jwt_and_bearer(self) -> None:
        self.assertNotIn(FAKE_JWT, redact(f"token is {FAKE_JWT}"))
        self.assertEqual(redact("Authorization: Bearer abcdef"), f"Authorization: Bearer {MASK}")

    def test_url_credentials(self) -> None:
        self.assertEqual(redact("mqtts://user:secret@broker:8883"), f"mqtts://user:{MASK}@broker:8883")

    def test_registered_secret(self) -> None:
        self.assertEqual(redact("connecting with hunter2-pass", ["hunter2-pass"]), f"connecting with {MASK}")

    def test_login_url_stays_readable(self) -> None:
        url = (
            "https://singlekey-id.com/auth/connect/authorize?client_id=X&response_type=code"
            "&code_challenge_method=S256&code_challenge=Q2hhbGxlbmdl&state=c3RhdGU"
        )
        self.assertEqual(redact(url), url)


class RedactionFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.addFilter(RedactionFilter(["FAKE-mqtt-password"]))
        self.logger = logging.getLogger("test.redaction")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)

    def tearDown(self) -> None:
        self.logger.removeHandler(self.handler)

    def test_args_message_and_exception_are_redacted(self) -> None:
        self.logger.info("refreshed: refresh_token=%s access=%s", REFRESH, FAKE_JWT)
        self.logger.warning("broker password %s", "FAKE-mqtt-password")
        try:
            raise ValueError(f"bad body code_verifier={VERIFIER}")
        except ValueError:
            self.logger.exception("exchange failed")
        output = self.stream.getvalue()
        for secret in (REFRESH, FAKE_JWT, VERIFIER, "FAKE-mqtt-password"):
            self.assertNotIn(secret, output)
        self.assertIn("exchange failed", output)
        self.assertIn("ValueError", output)

    def test_library_loggers_are_redacted_via_root_handler(self) -> None:
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            handler = setup_logging("debug")
            stream = io.StringIO()
            handler.setStream(stream)
            logging.getLogger("homecom_alt.base").debug("body refresh_token=%s", REFRESH)
            self.assertNotIn(REFRESH, stream.getvalue())
            self.assertIn("homecom_alt.base", stream.getvalue())
        finally:
            for current in list(root.handlers):
                root.removeHandler(current)
            for previous in saved_handlers:
                root.addHandler(previous)
            root.setLevel(saved_level)


if __name__ == "__main__":
    unittest.main()
