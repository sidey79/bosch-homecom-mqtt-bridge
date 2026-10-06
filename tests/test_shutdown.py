"""Real SIGTERM against the real ``run`` command in a subprocess (fake cloud and broker, no network)."""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from .token_fakes import read_auth_file, write_auth_file

ROOT = Path(__file__).resolve().parents[1]
GRACE = 20.0  # stop_grace_period


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ShutdownTest(unittest.TestCase):
    def test_sigterm_during_the_refresh_post_still_writes_auth_json_and_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            marker = Path(tmp) / "marker"
            write_auth_file(auth, 1, "FAKE-refresh-before", None)  # no access token: the first poll refreshes
            env = {
                **os.environ,
                "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}",
                "BOSCH_AUTH_PATH": str(auth),
                "CHILD_MARKER": str(marker),
                "HEALTH_PORT": str(free_port()),
                "LOG_LEVEL": "debug",
            }
            child = subprocess.Popen(
                [sys.executable, "-m", "tests.sigterm_child"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 30
                while not marker.exists():
                    self.assertIsNone(child.poll(), "child ended before the refresh POST")
                    self.assertLess(time.monotonic(), deadline, "refresh POST did not start")
                    time.sleep(0.05)
                started = time.monotonic()
                child.send_signal(signal.SIGTERM)
                output, _ = child.communicate(timeout=30)
                elapsed = time.monotonic() - started
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            saved = read_auth_file(auth)

        self.assertEqual(child.returncode, 0, output)
        self.assertLess(elapsed, GRACE)
        self.assertEqual(saved["refresh_token"], "FAKE-refresh-after-sigterm")
        self.assertEqual(saved["generation"], 2)
        self.assertIsNotNone(saved["access_token"])
        for secret in ("FAKE-refresh", saved["access_token"], "Bearer"):
            self.assertNotIn(secret, output)
        self.assertIn("Bridge stopped", output)


if __name__ == "__main__":
    unittest.main()
