import json
import unittest

from homecom_alt import HomeComAlt
from homecom_alt.model import BHCDeviceWddw2
from homecom_alt.wddw2 import HomeComWddw2

from bosch_homecom_mqtt_bridge import protocol
from bosch_homecom_mqtt_bridge.adapters import ADAPTERS, adapter_for, flatten_wddw2
from bosch_homecom_mqtt_bridge.topics import Topics

DEVICE = "101506113"


def sample() -> BHCDeviceWddw2:
    return BHCDeviceWddw2(
        device=DEVICE,
        firmware=[],
        notifications=[{"code": "x"}, {"code": "y"}],
        dhw_circuits=[
            {
                "id": "/dhwCircuits/dhw1",
                "operationMode": {"value": "eco", "allowedValues": ["off", "eco"]},
                "airBoxTemperature": {"value": 18.5, "unitOfMeasure": "C"},
                "fanSpeed": {"value": 1200},
                "inletTemperature": {"value": 12.5},
                "outletTemperature": {"value": 52.5},
                "waterFlow": {"value": float("nan")},
                "safetyTemperature": {},
                "nbStarts": {"value": 42},
                "tempLevel": {"manual": {"value": 50.0}, "bad/key": {"value": 1}},
            },
            {"id": "/dhwCircuits/other"},
        ],
        heat_sources={"actualPower": {"value": 3.2}, "operationHours": {"value": [1, 2]}},
        water_total_consumption={"value": 1234},
        holiday_mode={"value": True},
    )


class AdapterTest(unittest.TestCase):
    def test_registry(self) -> None:
        self.assertEqual(sorted(ADAPTERS), ["wddw2"])
        self.assertIs(adapter_for("wddw2"), ADAPTERS["wddw2"])
        for unknown in ("rac", None, 5, ""):
            self.assertIsNone(adapter_for(unknown))

    def test_flat_scalar_values_without_unit_text(self) -> None:
        values = flatten_wddw2(sample())
        self.assertEqual(values["dhw1_operation_mode"], "eco")
        self.assertEqual(values["dhw1_air_box_temperature"], 18.5)
        self.assertEqual(values["dhw1_inlet_temperature"], 12.5)
        self.assertEqual(values["dhw1_outlet_temperature"], 52.5)
        self.assertIsNone(values["dhw1_safety_temperature"])  # {} from a 404
        self.assertEqual(values["dhw1_temp_level_manual"], 50.0)
        self.assertNotIn("dhw1_temp_level_bad/key", values)
        self.assertEqual(values["hs_starts"], 42)
        self.assertEqual(values["hs_actual_power"], 3.2)
        self.assertIsNone(values["hs_operation_hours"])  # a list is not a scalar
        self.assertEqual(values["water_total_consumption"], 1234)
        self.assertIs(values["holiday_mode"], True)
        self.assertEqual(values["notifications"], 2)
        self.assertFalse([key for key in values if "other" in key])
        self.assertFalse([key for key in values if key.endswith(("_unit", "_c"))])

    def test_result_is_valid_flat_state_payload(self) -> None:
        message = protocol.state_message(Topics("bosch-homecom"), DEVICE, flatten_wddw2(sample()))
        payload = json.loads(message.payload)
        self.assertIsNone(payload["dhw1_water_flow"])  # NaN becomes null
        self.assertTrue(all(not isinstance(value, (dict, list)) for value in payload.values()))

    def test_empty_device_gives_stable_keys(self) -> None:
        values = flatten_wddw2(BHCDeviceWddw2(device=DEVICE, firmware=None, notifications=None, dhw_circuits=None))
        self.assertEqual(values["notifications"], None)
        self.assertIsNone(values["hs_actual_power"])

    def test_api_comes_from_the_fetch_factory(self) -> None:
        class Tokens:
            def fetch_api(self, cls, **kwargs):
                self.args = (cls, kwargs)
                return "api"

        tokens = Tokens()
        self.assertEqual(ADAPTERS["wddw2"].create_api(tokens, DEVICE), "api")  # type: ignore[arg-type]
        self.assertEqual(tokens.args, (HomeComWddw2, {"device_id": DEVICE}))
        self.assertTrue(issubclass(HomeComWddw2, HomeComAlt))


if __name__ == "__main__":
    unittest.main()
