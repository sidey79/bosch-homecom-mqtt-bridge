import unittest

from bosch_homecom_mqtt_bridge.topics import TopicError, Topics, validate_device_id, validate_segment


class SegmentValidationTest(unittest.TestCase):
    REJECTED = [
        ("", "empty"),
        ("a/b", "level separator"),
        ("/", "only separator"),
        ("dev+1", "single-level wildcard"),
        ("+", "bare single-level wildcard"),
        ("dev#", "multi-level wildcard"),
        ("#", "bare multi-level wildcard"),
        ("$SYS", "system topic prefix"),
        ("$", "bare dollar"),
        ("dev\x00ice", "NUL"),
    ]

    def test_rejected_segments(self) -> None:
        for value, reason in self.REJECTED:
            with self.subTest(reason=reason):
                with self.assertRaises(TopicError):
                    validate_segment(value)
                with self.assertRaises(TopicError):
                    validate_device_id(value)

    def test_error_message_does_not_echo_value(self) -> None:
        with self.assertRaises(TopicError) as ctx:
            validate_device_id("secret-ish/value")
        self.assertNotIn("secret-ish", str(ctx.exception))
        self.assertIn("device ID", str(ctx.exception))

    def test_accepted_segments(self) -> None:
        for value in ("101506113", "dev-1", "dev_1", "a$b", "Gerät", "dev.1"):
            with self.subTest(value=value):
                self.assertEqual(validate_device_id(value), value)

    def test_non_string_rejected(self) -> None:
        with self.assertRaises(TopicError):
            validate_device_id(101506113)  # type: ignore[arg-type]

    def test_event_is_reserved_as_device_id(self) -> None:
        with self.assertRaises(TopicError):
            validate_device_id("event")
        self.assertEqual(validate_segment("event"), "event")


class TopicsTest(unittest.TestCase):
    def test_topic_names(self) -> None:
        topics = Topics("bosch-homecom")
        self.assertEqual(topics.status, "bosch-homecom/event/status")
        self.assertEqual(topics.error, "bosch-homecom/event/error")
        self.assertEqual(topics.state("101506113"), "bosch-homecom/101506113/state")
        self.assertEqual(topics.availability("101506113"), "bosch-homecom/101506113/availability")

    def test_multi_level_base(self) -> None:
        topics = Topics("home/bosch")
        self.assertEqual(topics.status, "home/bosch/event/status")
        self.assertEqual(topics.state("1"), "home/bosch/1/state")

    def test_invalid_base_rejected(self) -> None:
        for base in ("", "home/", "/home", "home//bosch", "home/+", "#", "$SYS/bridge", "a\x00b"):
            with self.subTest(base=base):
                with self.assertRaises(TopicError):
                    Topics(base)

    def test_device_topics_reject_wildcards(self) -> None:
        topics = Topics("bosch-homecom")
        for device_id in ("a/b", "a+b", "a#b", "$x", "event"):
            with self.subTest(device_id=device_id):
                with self.assertRaises(TopicError):
                    topics.state(device_id)
                with self.assertRaises(TopicError):
                    topics.availability(device_id)


if __name__ == "__main__":
    unittest.main()
