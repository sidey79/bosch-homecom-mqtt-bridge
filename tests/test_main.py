import unittest
from unittest import mock

from bosch_homecom_mqtt_bridge import __main__ as entry


class MainTest(unittest.TestCase):
    def test_run_is_the_default_command(self) -> None:
        for argv in ([], ["run"]):
            with self.subTest(argv=argv), mock.patch.object(entry, "_run", return_value=0) as run:
                self.assertEqual(entry.main(argv), 0)
                run.assert_called_once_with()

    def test_login_does_not_start_the_bridge(self) -> None:
        with mock.patch.object(entry, "_run") as run, mock.patch.object(entry, "_login", return_value=0):
            self.assertEqual(entry.main(["login"]), 0)
        run.assert_not_called()

    def test_logging_is_set_up_before_the_bridge_starts(self) -> None:
        order: list[str] = []

        def fake_run(config):
            order.append("run")
            return "coro"

        with (
            mock.patch.object(entry, "setup_logging", side_effect=lambda *a: order.append("logging")),
            mock.patch("bosch_homecom_mqtt_bridge.app.run", new=fake_run),
            mock.patch.object(entry.asyncio, "run", return_value=0),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            self.assertEqual(entry._run(), 0)
        self.assertEqual(order, ["logging", "run"])

    def test_a_failure_prints_the_type_only(self) -> None:
        with mock.patch.object(entry, "_run", side_effect=RuntimeError("Bearer FAKE-secret")), mock.patch(
            "sys.stderr"
        ) as err:
            self.assertEqual(entry.main([]), 1)
        printed = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("RuntimeError", printed)
        self.assertNotIn("FAKE-secret", printed)


if __name__ == "__main__":
    unittest.main()
