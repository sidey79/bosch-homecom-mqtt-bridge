"""Device adapters: one per device type, read-only (D3).

An adapter builds the ``homecom_alt`` client for its type and flattens the data it reads into a flat
dict for ``<base>/<deviceId>/state`` (MQTT contract). The client comes from the token manager's fetch
factory (``auth_provider=False``), so reading never refreshes a token (R-b, ADR 0001).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Protocol

from homecom_alt import HomeComAlt
from homecom_alt.wddw2 import HomeComWddw2

from .auth.token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)
_DHW_ID = re.compile(r"dhw\d")


class Adapter(Protocol):
    device_type: str

    def create_api(self, tokens: TokenManager, device_id: str) -> HomeComAlt: ...

    async def read(self, api: Any, device_id: str) -> dict[str, object]: ...


def _scalar(resource: object) -> object:
    """``value`` of a cloud resource if it is a scalar, else None (nested values are not published)."""
    value = resource.get("value") if isinstance(resource, Mapping) else None
    return value if isinstance(value, (bool, int, float, str)) else None


def flatten_wddw2(data: Any) -> dict[str, object]:
    """Flat values of a ``BHCDeviceWddw2``.

    Values are published as the cloud delivers them (temperatures in °C); the keys carry no unit text.
    No unit conversion is applied: the unit strings of the real device are not verified yet.
    """
    values: dict[str, object] = {}
    for ref in data.dhw_circuits if isinstance(data.dhw_circuits, list) else []:
        dhw = str(ref.get("id", "")).split("/")[-1] if isinstance(ref, Mapping) else ""
        if not _DHW_ID.fullmatch(dhw):
            continue
        for name, key in (
            ("operationMode", "operation_mode"),
            ("airBoxTemperature", "air_box_temperature"),
            ("fanSpeed", "fan_speed"),
            ("inletTemperature", "inlet_temperature"),
            ("outletTemperature", "outlet_temperature"),
            ("waterFlow", "water_flow"),
            ("safetyTemperature", "safety_temperature"),
        ):
            values[f"{dhw}_{key}"] = _scalar(ref.get(name))
        levels = ref.get("tempLevel")
        for level, resource in (levels if isinstance(levels, Mapping) else {}).items():
            if re.fullmatch(r"[A-Za-z0-9]+", str(level)):
                values[f"{dhw}_temp_level_{level}"] = _scalar(resource)
        if "nbStarts" in ref:
            values["hs_starts"] = _scalar(ref["nbStarts"])
    sources = data.heat_sources if isinstance(data.heat_sources, Mapping) else {}
    for name, key in (
        ("actualPower", "hs_actual_power"),
        ("powerPercentage", "hs_power_percentage"),
        ("operationHours", "hs_operation_hours"),
        ("electricityTotalConsumption", "hs_electricity_total_consumption"),
    ):
        values[key] = _scalar(sources.get(name))
    values["water_total_consumption"] = _scalar(data.water_total_consumption)
    values["holiday_mode"] = _scalar(data.holiday_mode)
    values["notifications"] = len(data.notifications) if isinstance(data.notifications, list) else None
    return values


class Wddw2Adapter:
    device_type = "wddw2"

    def create_api(self, tokens: TokenManager, device_id: str) -> HomeComAlt:
        return tokens.fetch_api(HomeComWddw2, device_id=device_id)

    async def read(self, api: Any, device_id: str) -> dict[str, object]:
        return flatten_wddw2(await api.async_update(device_id))


ADAPTERS: dict[str, Adapter] = {adapter.device_type: adapter for adapter in (Wddw2Adapter(),)}


def adapter_for(device_type: object) -> Adapter | None:
    return ADAPTERS.get(device_type) if isinstance(device_type, str) else None
