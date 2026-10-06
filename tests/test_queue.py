import unittest
from datetime import datetime, timezone

from bosch_homecom_mqtt_bridge import protocol
from bosch_homecom_mqtt_bridge.protocol import Kind, Message
from bosch_homecom_mqtt_bridge.publish_queue import PublishQueue
from bosch_homecom_mqtt_bridge.topics import Topics

TOPICS = Topics("bosch-homecom")
MOMENT = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def state(n: int) -> Message:
    return protocol.state_message(TOPICS, "dev", {"n": n}, MOMENT)


def error(n: int) -> Message:
    return protocol.error_message(TOPICS, "AUTH_REQUIRED", f"error {n}")


def status(state_name: str = "ready") -> Message:
    return protocol.status_message(TOPICS, state_name)


def availability(online: bool = True) -> Message:
    return protocol.availability_message(TOPICS, "dev", online)


def availability_of(device_id: str, online: bool) -> Message:
    return protocol.availability_message(TOPICS, device_id, online)


class PublishQueueTest(unittest.TestCase):
    def test_fifo_below_limit(self) -> None:
        queue = PublishQueue(3)
        items = [status(), state(1), error(1)]
        for item in items:
            self.assertIsNone(queue.put(item))
        self.assertEqual([queue.pop() for _ in range(3)], items)
        self.assertFalse(queue)

    def test_default_size(self) -> None:
        self.assertEqual(PublishQueue().maxsize, 1000)

    def test_invalid_size(self) -> None:
        with self.assertRaises(ValueError):
            PublishQueue(0)

    def test_oldest_state_dropped_first(self) -> None:
        queue = PublishQueue(4)
        for item in (state(1), error(1), state(2), status()):
            queue.put(item)
        dropped = queue.put(state(3))
        self.assertEqual(dropped, state(1))
        self.assertEqual(queue.snapshot(), [error(1), state(2), status(), state(3)])
        self.assertEqual(len(queue), 4)

    def test_state_dropped_before_error_even_if_error_is_older(self) -> None:
        queue = PublishQueue(3)
        for item in (error(1), error(2), state(1)):
            queue.put(item)
        self.assertEqual(queue.put(error(3)), state(1))
        self.assertEqual(queue.snapshot(), [error(1), error(2), error(3)])

    def test_oldest_error_dropped_when_no_state_left(self) -> None:
        queue = PublishQueue(3)
        for item in (error(1), status(), error(2)):
            queue.put(item)
        self.assertEqual(queue.put(error(3)), error(1))
        self.assertEqual(queue.snapshot(), [status(), error(2), error(3)])

    def test_incoming_state_dropped_when_only_errors_and_protected_left(self) -> None:
        queue = PublishQueue(2)
        queue.put(error(1))
        queue.put(availability())
        incoming = state(1)
        self.assertIs(queue.put(incoming), incoming)
        self.assertEqual(queue.snapshot(), [error(1), availability()])

    def test_incoming_error_dropped_when_only_protected_left(self) -> None:
        queue = PublishQueue(2)
        queue.put(status())
        queue.put(availability())
        incoming = error(1)
        self.assertIs(queue.put(incoming), incoming)
        self.assertEqual(len(queue), 2)

    def test_status_and_availability_never_dropped(self) -> None:
        queue = PublishQueue(2)
        protected = [status("starting"), availability(True), availability_of("other", True)]
        for item in protected:
            self.assertIsNone(queue.put(item))
        self.assertEqual(queue.snapshot(), protected)
        self.assertEqual(len(queue), 3)

    def test_status_and_availability_coalesced_per_topic(self) -> None:
        queue = PublishQueue(10)
        for item in (status("starting"), availability(True), state(1), status("ready"), availability(False)):
            self.assertIsNone(queue.put(item))
        self.assertEqual(queue.snapshot(), [state(1), status("ready"), availability(False)])

    def test_state_and_error_not_coalesced(self) -> None:
        queue = PublishQueue(10)
        for item in (state(1), state(2), error(1), error(1)):
            queue.put(item)
        self.assertEqual(len(queue), 4)

    def test_status_evicts_state_then_error_at_limit(self) -> None:
        queue = PublishQueue(2)
        queue.put(error(1))
        queue.put(state(1))
        self.assertEqual(queue.put(status()), state(1))
        self.assertEqual(queue.put(availability()), error(1))
        self.assertEqual(queue.snapshot(), [status(), availability()])

    def test_overflow_burst_stays_bounded_and_keeps_latest_status(self) -> None:
        queue = PublishQueue(10)
        dropped: list[Message] = []
        for n in range(200):
            for item in (state(n), error(n)) if n % 2 else (state(n), status("ready" if n % 4 else "error")):
                result = queue.put(item)
                if result is not None:
                    dropped.append(result)
        self.assertNotIn(Kind.STATUS, {m.kind for m in dropped})
        self.assertLessEqual(len(queue), 10)
        statuses = [m for m in queue.snapshot() if m.kind is Kind.STATUS]
        self.assertEqual(statuses, [status("ready")])  # the last status put (n = 198)

    def test_push_front_never_drops(self) -> None:
        queue = PublishQueue(1)
        queue.put(state(1))
        queue.push_front(status())
        self.assertEqual(queue.snapshot(), [status(), state(1)])
        self.assertEqual(queue.pop(), status())

    def test_push_front_keeps_newer_message_of_same_topic(self) -> None:
        queue = PublishQueue(5)
        queue.put(state(1))
        queue.put(status("error"))
        queue.push_front(status("ready"))  # older status handed back after a failed hand-over
        self.assertEqual(queue.snapshot(), [state(1), status("error")])


if __name__ == "__main__":
    unittest.main()
