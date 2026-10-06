import unittest
from pathlib import Path

from bosch_homecom_mqtt_bridge.config import ConfigError, load_config

PASSWORD = "FAKE-mqtt-password-value"


class ConfigDefaultsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = load_config({})
        self.assertEqual(config.mqtt_url, "mqtt://localhost:1883")
        self.assertIsNone(config.mqtt_username)
        self.assertIsNone(config.mqtt_password)
        self.assertEqual(config.mqtt_base_topic, "bosch-homecom")
        self.assertEqual(config.mqtt_client_id, "bosch-homecom-mqtt-bridge")
        self.assertEqual(config.mqtt_queue_size, 1000)
        self.assertEqual(config.bosch_auth_path, Path("/data/auth.json"))
        self.assertIsNone(config.bosch_device_id)
        self.assertEqual(config.bosch_brand, "bosch")
        self.assertEqual(config.bosch_poll_interval, 60)
        self.assertEqual(config.health_port, 8080)
        self.assertEqual(config.log_level, "info")

    def test_empty_values_count_as_unset(self) -> None:
        config = load_config({"MQTT_USERNAME": "", "MQTT_PASSWORD": "  ", "BOSCH_POLL_INTERVAL": ""})
        self.assertIsNone(config.mqtt_username)
        self.assertIsNone(config.mqtt_password)
        self.assertEqual(config.bosch_poll_interval, 60)

    def test_explicit_values(self) -> None:
        config = load_config(
            {
                "MQTT_URL": "mqtts://broker.example:8883",
                "MQTT_USERNAME": "bridge",
                "MQTT_PASSWORD": PASSWORD,
                "MQTT_BASE_TOPIC": "heating",
                "MQTT_CLIENT_ID": "bridge-1",
                "BOSCH_AUTH_PATH": "/tmp/state/auth.json",
                "BOSCH_DEVICE_ID": "101506113",
                "BOSCH_BRAND": "Buderus",
                "BOSCH_POLL_INTERVAL": "120",
                "HEALTH_PORT": "9000",
                "LOG_LEVEL": "DEBUG",
            }
        )
        self.assertEqual(config.mqtt_url, "mqtts://broker.example:8883")
        self.assertEqual(config.mqtt_password, PASSWORD)
        self.assertEqual(config.bosch_auth_path, Path("/tmp/state/auth.json"))
        self.assertEqual(config.bosch_device_id, "101506113")
        self.assertEqual(config.bosch_brand, "buderus")
        self.assertEqual(config.bosch_poll_interval, 120)
        self.assertEqual(config.health_port, 9000)
        self.assertEqual(config.log_level, "debug")

    def test_password_not_in_repr(self) -> None:
        config = load_config({"MQTT_PASSWORD": PASSWORD})
        self.assertNotIn(PASSWORD, repr(config))
        self.assertNotIn("mqtt_url", repr(config))


class ConfigValidationTest(unittest.TestCase):
    def assert_rejected(self, env: dict[str, str], name: str) -> ConfigError:
        with self.assertRaises(ConfigError) as ctx:
            load_config({"MQTT_PASSWORD": PASSWORD, **env})
        message = str(ctx.exception)
        self.assertIn(name, message)
        self.assertNotIn(PASSWORD, message)
        for value in env.values():
            self.assertNotIn(value, message)
        return ctx.exception

    def test_poll_interval_bounds(self) -> None:
        self.assertEqual(load_config({"BOSCH_POLL_INTERVAL": "30"}).bosch_poll_interval, 30)
        self.assertEqual(load_config({"BOSCH_POLL_INTERVAL": "3600"}).bosch_poll_interval, 3600)
        self.assert_rejected({"BOSCH_POLL_INTERVAL": "29"}, "BOSCH_POLL_INTERVAL")
        self.assert_rejected({"BOSCH_POLL_INTERVAL": "3601"}, "BOSCH_POLL_INTERVAL")
        self.assert_rejected({"BOSCH_POLL_INTERVAL": "sixty"}, "BOSCH_POLL_INTERVAL")

    def test_brand(self) -> None:
        self.assert_rejected({"BOSCH_BRAND": "foo"}, "BOSCH_BRAND")

    def test_health_port(self) -> None:
        self.assert_rejected({"HEALTH_PORT": "0"}, "HEALTH_PORT")
        self.assert_rejected({"HEALTH_PORT": "65536"}, "HEALTH_PORT")

    def test_log_level(self) -> None:
        self.assert_rejected({"LOG_LEVEL": "verbose"}, "LOG_LEVEL")

    def test_mqtt_url(self) -> None:
        self.assert_rejected({"MQTT_URL": "http://broker:1883"}, "MQTT_URL")
        self.assert_rejected({"MQTT_URL": "mqtt://:1883"}, "MQTT_URL")
        self.assert_rejected({"MQTT_URL": "mqtt://broker:notaport"}, "MQTT_URL")

    def test_mqtt_url_with_credentials_is_not_echoed(self) -> None:
        self.assert_rejected({"MQTT_URL": f"tcp://user:{PASSWORD}@broker:1883"}, "MQTT_URL")

    def test_mqtt_url_credentials_are_rejected_with_hint(self) -> None:
        for url in (f"mqtt://user:{PASSWORD}@broker:1883", f"mqtts://:{PASSWORD}@broker", "mqtt://user@broker"):
            with self.subTest(url=url):
                error = self.assert_rejected({"MQTT_URL": url}, "MQTT_URL")
                self.assertIn("MQTT_USERNAME", str(error))
                self.assertIn("MQTT_PASSWORD", str(error))

    def test_mqtt_url_path_query_fragment_and_port_zero_are_rejected(self) -> None:
        for url in (
            "mqtt://broker:1883/topic",
            "mqtt://broker:1883?x=1",
            "mqtt://broker:1883#frag",
            "mqtt://broker?",
            "mqtt://broker:0",
        ):
            with self.subTest(url=url):
                self.assert_rejected({"MQTT_URL": url}, "MQTT_URL")
        self.assertEqual(load_config({"MQTT_URL": "mqtt://broker:1883/"}).mqtt_url, "mqtt://broker:1883/")

    def test_device_id(self) -> None:
        self.assertEqual(load_config({"BOSCH_DEVICE_ID": "a_B-9"}).bosch_device_id, "a_B-9")
        self.assertEqual(load_config({"BOSCH_DEVICE_ID": "x" * 64}).bosch_device_id, "x" * 64)
        for value in ("x" * 65, "101/506", "id with space", "../etc", "id\nx"):
            with self.subTest(value=value):
                self.assert_rejected({"BOSCH_DEVICE_ID": value}, "BOSCH_DEVICE_ID")

    def test_base_topic(self) -> None:
        self.assertEqual(load_config({"MQTT_BASE_TOPIC": "home/heating"}).mqtt_base_topic, "home/heating")
        for value in ("heating/#", "heat+ing", "$SYS/heating", "heat\x00ing", "/heating", "heating/", "home//heating", "home/$SYS"):
            with self.subTest(value=value):
                self.assert_rejected({"MQTT_BASE_TOPIC": value}, "MQTT_BASE_TOPIC")

    def test_client_id(self) -> None:
        self.assertEqual(load_config({"MQTT_CLIENT_ID": "$bridge"}).mqtt_client_id, "$bridge")
        for value in ("bridge#1", "bridge+1", "bridge\x001"):
            with self.subTest(value=value):
                self.assert_rejected({"MQTT_CLIENT_ID": value}, "MQTT_CLIENT_ID")

    def test_poll_timeout_bounds(self) -> None:
        self.assertEqual(load_config({}).bosch_poll_timeout, 300)
        self.assertEqual(load_config({"BOSCH_POLL_TIMEOUT": ""}).bosch_poll_timeout, 300)
        self.assertEqual(load_config({"BOSCH_POLL_TIMEOUT": "60"}).bosch_poll_timeout, 60)
        self.assertEqual(load_config({"BOSCH_POLL_TIMEOUT": "900"}).bosch_poll_timeout, 900)
        for value in ("59", "901", "5min"):
            with self.subTest(value=value):
                self.assert_rejected({"BOSCH_POLL_TIMEOUT": value}, "BOSCH_POLL_TIMEOUT")

    def test_env_example_lists_poll_timeout_default(self) -> None:
        lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
        self.assertIn("BOSCH_POLL_TIMEOUT=300", lines)

    def test_queue_size_bounds(self) -> None:
        self.assertEqual(load_config({"MQTT_QUEUE_SIZE": "10"}).mqtt_queue_size, 10)
        self.assertEqual(load_config({"MQTT_QUEUE_SIZE": "100000"}).mqtt_queue_size, 100000)
        self.assert_rejected({"MQTT_QUEUE_SIZE": "9"}, "MQTT_QUEUE_SIZE")
        self.assert_rejected({"MQTT_QUEUE_SIZE": "100001"}, "MQTT_QUEUE_SIZE")


if __name__ == "__main__":
    unittest.main()
