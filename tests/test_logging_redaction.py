import io
import logging
import sys
import threading
import time
import unittest

import jwt

from bosch_homecom_mqtt_bridge.logging_setup import (
    MASK,
    MAX_RUNTIME_SECRETS,
    RedactionFilter,
    _longest_first,
    add_secret,
    redact,
    setup_logging,
)

# Generated at runtime so no JWT literal is checked in.
FAKE_JWT = jwt.encode({"exp": 1}, "test-signing-key-for-fake-jwts-only-0123456789", algorithm="HS256")
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
            f"mqtt_password={REFRESH}",
            f'{{"refreshToken": "{REFRESH}"}}',
            f"client_secret={REFRESH}",
            f"auth_code={CODE}",
            f"codeVerifier: {VERIFIER}",
            f"{{'access_token': '{REFRESH}'}}",
            f'{{\\"refresh_token\\": \\"{REFRESH}\\", \\"brand\\": \\"bosch\\"}}',
            f"api_key={REFRESH}",
            f"X-Api-Key: {REFRESH}",
            f'{{"apiKey": "{REFRESH}"}}',
            f"passwordHash={REFRESH}",
            f'{{"password_hash": "{REFRESH}"}}',
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                result = redact(sample)
                for secret in (REFRESH, VERIFIER, CODE):
                    self.assertNotIn(secret, result)
                self.assertIn(MASK, result)

    def test_quoted_values_are_masked_completely(self) -> None:
        self.assertEqual(redact('password="FAKE secret with spaces"'), f'password="{MASK}"')
        self.assertEqual(redact("password='FAKE it\\'s quoted'"), f"password='{MASK}'")
        self.assertEqual(redact('{"client_secret": "FAKE a, b; c}"}'), f'{{"client_secret": "{MASK}"}}')

    def test_truncated_quoted_values_are_masked(self) -> None:
        # A value cut off by a length limit has no closing quote.
        self.assertEqual(redact(f'{{"refresh_token": "{REFRESH}'), f'{{"refresh_token": "{MASK}')
        self.assertEqual(redact(f"{{'refresh_token': '{REFRESH}"), f"{{'refresh_token': '{MASK}")
        self.assertEqual(redact(f'{{\\"refresh_token\\": \\"{REFRESH}'), f'{{\\"refresh_token\\": \\"{MASK}')

    def test_escaped_json_keeps_structure(self) -> None:
        text = f'body={{\\"refresh_token\\": \\"{REFRESH}\\", \\"brand\\": \\"bosch\\"}}'
        expected = f'body={{\\"refresh_token\\": \\"{MASK}\\", \\"brand\\": \\"bosch\\"}}'
        self.assertEqual(redact(text), expected)

    def test_doubly_escaped_json_keeps_structure(self) -> None:
        q = '\\\\\\"'  # a doubly escaped quote: \\\"
        text = f"body={{{q}refresh_token{q}: {q}{REFRESH}{q}, {q}brand{q}: {q}bosch{q}}}"
        expected = text.replace(REFRESH, MASK)
        self.assertEqual(redact(text), expected)

    def test_value_with_trailing_backslash_is_masked(self) -> None:
        # A value ending in "\" (or with "\" before a line break) is still a quoted value.
        self.assertEqual(redact(f'{{"refresh_token": "{REFRESH}\\'), f'{{"refresh_token": "{MASK}')
        self.assertEqual(redact(f"{{'refresh_token': '{REFRESH}\\"), f"{{'refresh_token': '{MASK}")
        self.assertEqual(redact(f'{{\\"refresh_token\\": \\"{REFRESH}\\'), f'{{\\"refresh_token\\": \\"{MASK}')
        before_line_break = '{"refresh_token": "' + REFRESH + "\\\nFAKE-tail" + '", "n": 1}'
        self.assertEqual(redact(before_line_break), f'{{"refresh_token": "{MASK}", "n": 1}}')

    def test_bare_value_with_semicolon_is_masked_completely(self) -> None:
        self.assertEqual(redact("pwd=FAKE-a;FAKE-b next"), f"pwd={MASK} next")

    def test_cookies(self) -> None:
        self.assertEqual(redact("Cookie: sid=FAKE-c1; other=FAKE-c2"), f"Cookie: {MASK}")
        self.assertEqual(
            redact("Set-Cookie: sid=FAKE-c1; Path=/; HttpOnly\nnext line"), f"Set-Cookie: {MASK}\nnext line"
        )
        self.assertEqual(redact('{"set-cookie": "sid=FAKE-c1; x=y", "n": 1}'), f'{{"set-cookie": "{MASK}", "n": 1}}')

    def test_unquoted_cookie_is_masked_to_end_of_line(self) -> None:
        # Quotes and backslashes inside an unquoted cookie do not end it.
        self.assertEqual(redact("Cookie: sid=FAKE-a\"b'c\\d; x=FAKE-e\nnext"), f"Cookie: {MASK}\nnext")
        self.assertEqual(redact("Set-Cookie: sid=FAKE-a\\"), f"Set-Cookie: {MASK}")

    def test_quoted_cookie_is_masked_completely(self) -> None:
        self.assertEqual(
            redact("{'Cookie': 'sid=FAKE-a; q=\\'FAKE-b\\'', 'Host': 'h'}"), f"{{'Cookie': '{MASK}', 'Host': 'h'}}"
        )
        self.assertEqual(
            redact('{"cookie": "sid=FAKE-a; q=\\"FAKE-b\\"", "n": 1}'), f'{{"cookie": "{MASK}", "n": 1}}'
        )
        self.assertEqual(
            redact('{\\"set-cookie\\": \\"sid=FAKE-a\\", \\"n\\": 1}'),
            f'{{\\"set-cookie\\": \\"{MASK}\\", \\"n\\": 1}}',
        )
        self.assertEqual(redact('{"cookie": "sid=FAKE-cut'), f'{{"cookie": "{MASK}')

    def test_cookie_json_list_is_masked_completely(self) -> None:
        self.assertEqual(
            redact('{"set-cookie": ["sid=FAKE-a; Path=/", "b=FAKE-b]x"], "n": 1}'),
            f'{{"set-cookie": [{MASK}], "n": 1}}',
        )
        self.assertEqual(redact('{"set-cookie": ["sid=FAKE-cut'), f'{{"set-cookie": [{MASK}')

    def test_jwt_and_bearer(self) -> None:
        self.assertNotIn(FAKE_JWT, redact(f"token is {FAKE_JWT}"))
        self.assertEqual(redact("Authorization: Bearer abcdef"), f"Authorization: Bearer {MASK}")

    def test_authorization_basic(self) -> None:
        self.assertEqual(redact("Authorization: Basic dXNlcjpwYXNz"), f"Authorization: Basic {MASK}")
        self.assertEqual(redact("{'Authorization': 'Basic dXNlcjpwYXNz'}"), f"{{'Authorization': 'Basic {MASK}'}}")

    def test_url_credentials(self) -> None:
        self.assertEqual(redact("mqtts://user:secret@broker:8883"), f"mqtts://user:{MASK}@broker:8883")
        self.assertEqual(redact("mqtt://:secret@broker"), f"mqtt://:{MASK}@broker")
        self.assertEqual(redact("mqtt://user:se%2Fcr%2Fet@broker:1883"), f"mqtt://user:{MASK}@broker:1883")
        # A raw "@" in the password: masked up to the last "@" of the authority, nothing leaks.
        self.assertEqual(redact("mqtt://user:FAKE@pass@broker:1883/x"), f"mqtt://user:{MASK}@broker:1883/x")

    def test_url_password_stays_within_authority(self) -> None:
        # Port, path and a query containing "@" are not credentials.
        for text in ("https://host:8443/p?mail=a@b.com", "https://host:8443/users/a@b.com", "http://h:1#x@y"):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_long_url_password_is_masked(self) -> None:
        password = "FAKE-" + "p" * 1000  # up to 1024 characters
        self.assertEqual(redact(f"mqtt://user:{password}@broker"), f"mqtt://user:{MASK}@broker")

    def test_patterns_are_not_quadratic(self) -> None:
        def runtime(text: str) -> float:
            best = float("inf")
            for _ in range(3):  # the fastest run is the least disturbed by other load
                started = time.perf_counter()
                self.assertEqual(redact(text), text)
                best = min(best, time.perf_counter() - started)
            return best

        for unit in ("a://:", "a://:" + "x" * 30, "pwd\\"):  # scheme starts without "@", keys without values
            with self.subTest(unit=unit):
                single, double = runtime(unit * 8000), runtime(unit * 16000)
                # Linear: twice the input takes about twice as long; quadratic would be four times.
                self.assertTrue(
                    double < 4 * single or double < 0.05, f"n: {single:.4f} s, 2n: {double:.4f} s"
                )
                self.assertLess(double, 1.0)

    def test_secrets_are_sorted_longest_first_and_deterministically(self) -> None:
        self.assertEqual(_longest_first(["bb", "a", "ccc", "aa", "bb"]), ("ccc", "aa", "bb", "a"))
        self.assertEqual(redact("FAKE-secret-long", ["FAKE-secret", "FAKE-secret-long"]), MASK)

    def test_harmless_text_is_not_masked(self) -> None:
        for text in (
            "status code: 400",
            "Endpoint returned status code=502",
            "token_type=Bearer-less expires_in=3600",
            "response_type=code&code_challenge_method=S256",
            "Authorization has failed",
            "cookies are disabled",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

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

    def test_add_secret_at_runtime(self) -> None:
        redaction = RedactionFilter()
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "got %s", ("FAKE-late-secret",), None)
        redaction.add_secret("FAKE-late-secret")
        redaction.add_secret(None)
        redaction.add_secret("")
        redaction.filter(record)
        self.assertEqual(record.getMessage(), f"got {MASK}")

    def test_runtime_secrets_are_validated(self) -> None:
        redaction = RedactionFilter(["pw"])  # fixed secrets have no minimum length
        for value in ("short", "1234567", b"FAKE-bytes-secret", 12345678, ["FAKE-list-secret"]):
            redaction.add_secret(value)  # ignored, never raises
        redaction.add_secret("12345678")
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "short 1234567 12345678 pw", None, None)
        redaction.filter(record)
        self.assertEqual(record.getMessage(), f"short 1234567 {MASK} {MASK}")

    def test_runtime_secrets_are_bounded_and_fixed_ones_stay(self) -> None:
        redaction = RedactionFilter(["FAKE-fixed-pw"])
        added = [f"FAKE-runtime-{n:04d}" for n in range(MAX_RUNTIME_SECRETS + 5)]
        for secret in added:
            redaction.add_secret(secret)
        record = logging.LogRecord("x", logging.INFO, __file__, 1, " ".join(["FAKE-fixed-pw", *added]), None, None)
        redaction.filter(record)
        words = record.getMessage().split()
        self.assertEqual(words[0], MASK)
        # The five oldest runtime secrets fell out; the newest MAX_RUNTIME_SECRETS are masked.
        self.assertEqual(words[1:6], added[:5])
        self.assertEqual(words[6:], [MASK] * MAX_RUNTIME_SECRETS)

    def test_concurrent_add_secret_and_logging(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        redaction = RedactionFilter(["FAKE-fixed-pw"])
        handler.addFilter(redaction)
        logger = logging.getLogger("test.redaction.threads")
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        errors: list[BaseException] = []
        start, churned = threading.Barrier(8), threading.Barrier(8)
        finals = [f"FAKE-final-{n}" for n in range(4)]
        registered = [threading.Event() for _ in finals]

        def writer(n: int) -> None:
            try:
                start.wait()
                for i in range(500):
                    redaction.add_secret(f"FAKE-thread-{n}-{i:04d}")
                churned.wait()
                # The four final secrets are added at the same time; a lost update drops one.
                redaction.add_secret(finals[n])
                registered[n].set()
            except BaseException as error:  # reported by the main thread
                errors.append(error)

        def reader(n: int) -> None:
            try:
                start.wait()
                for i in range(300):
                    logger.info("reader %d line %d: FAKE-fixed-pw", n, i)
                churned.wait()
                # A secret whose add_secret has returned must be masked from then on, also while
                # the other writers are still adding theirs.
                for m, final in enumerate(finals):
                    registered[m].wait(timeout=10)
                    for i in range(20):
                        logger.info("reader %d final %d line %d: %s", n, m, i, final)
            except BaseException as error:  # reported by the main thread
                errors.append(error)

        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # force frequent thread switches
        try:
            threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
            threads += [threading.Thread(target=reader, args=(n,)) for n in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            sys.setswitchinterval(interval)
            logger.removeHandler(handler)
        self.assertEqual(errors, [])
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 4 * 300 + 4 * 4 * 20)
        self.assertNotIn("FAKE-", stream.getvalue())
        # Every writer's last value is in the snapshot and masked: catches a no-op add_secret and,
        # most of the time, a lost update without the lock.
        self.assertEqual(sorted(s for s in redaction._snapshot if s.startswith("FAKE-final-")), finals)
        record = logging.LogRecord("x", logging.INFO, __file__, 1, " ".join(finals), None, None)
        redaction.filter(record)
        self.assertEqual(record.getMessage(), " ".join([MASK] * 4))
        # After the churn the filter still works and masks a newly added secret.
        redaction.add_secret("FAKE-after-churn")
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "FAKE-after-churn FAKE-fixed-pw", None, None)
        redaction.filter(record)
        self.assertEqual(record.getMessage(), f"{MASK} {MASK}")

    def test_broken_format_arguments_neither_raise_nor_leak(self) -> None:
        redaction = RedactionFilter(["FAKE-registered"])
        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "refresh_token=%s FAKE-registered %d", ("FAKE-arg",), None
        )
        self.assertTrue(redaction.filter(record))
        message = record.getMessage()
        self.assertNotIn("FAKE-registered", message)
        self.assertIn("unformattable", message)

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
            add_secret("FAKE-registered-later")
            logging.getLogger("homecom_alt.base").debug("plain FAKE-registered-later")
            self.assertNotIn(REFRESH, stream.getvalue())
            self.assertNotIn("FAKE-registered-later", stream.getvalue())
            self.assertIn("homecom_alt.base", stream.getvalue())
        finally:
            for current in list(root.handlers):
                root.removeHandler(current)
            for previous in saved_handlers:
                root.addHandler(previous)
            root.setLevel(saved_level)


if __name__ == "__main__":
    unittest.main()
