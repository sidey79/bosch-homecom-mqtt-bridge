import re
import subprocess
import sys
import unittest
from pathlib import Path

import bosch_homecom_mqtt_bridge

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


class VersionTest(unittest.TestCase):
    def test_version_file_is_semver(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, SEMVER)

    def test_package_reports_version_file(self) -> None:
        self.assertEqual(bosch_homecom_mqtt_bridge.__version__, (ROOT / "VERSION").read_text(encoding="utf-8").strip())

    def test_cli_version(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "bosch_homecom_mqtt_bridge", "--version"],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), f"bosch-homecom-mqtt-bridge {bosch_homecom_mqtt_bridge.__version__}")


if __name__ == "__main__":
    unittest.main()
