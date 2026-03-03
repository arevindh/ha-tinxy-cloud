"""
Tinxy Sensor Platform – diagnostic sensors (RSSI, IP address).

Two diagnostic sensors are created per physical Tinxy device:
  • RSSI  – signal strength in dBm
  • IP    – current IP address on the local network

Both values arrive via MQTT /info messages.  Devices that never send these
fields (e.g. wired locks, EVA hub) will show state "unavailable" until a
message with the relevant field is received.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TinxyUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create RSSI and IP diagnostic sensors per physical Tinxy device."""
    apidata = hass.data[DOMAIN][entry.entry_id][0]
    coordinator: TinxyUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][1]

    await coordinator.async_config_entry_first_refresh()

    seen: set[str] = set()
    entities: list[SensorEntity] = []

    for device in apidata.list_all_devices():
        device_id: str = device["device_id"]
        if device_id in seen:
            continue
        seen.add(device_id)

        relay_key = f"{device_id}-1"
        if relay_key not in (coordinator.data or {}):
            continue

        entities.append(TinxyRSSISensor(coordinator, relay_key))
        entities.append(TinxyIPSensor(coordinator, relay_key))

    async_add_entities(entities)
    _LOGGER.info("Tinxy: added %d diagnostic sensor(s).", len(entities))


class _TinxyDiagnosticSensor(CoordinatorEntity, SensorEntity):
    """Shared base for Tinxy diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TinxyUpdateCoordinator, relay_key: str) -> None:
        super().__init__(coordinator)
        self._relay_key = relay_key

    @property
    def _data(self) -> Dict[str, Any]:
        return self.coordinator.data.get(self._relay_key, {})

    @property
    def _device_name(self) -> str:
        return (
            self._data.get("device", {}).get("name")
            or self._data.get("name", "Tinxy")
        )

    @property
    def device_info(self) -> Dict[str, Any]:
        return self._data.get("device", {})

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class TinxyRSSISensor(_TinxyDiagnosticSensor):
    """Signal strength (RSSI) diagnostic sensor.

    Shows dBm value from MQTT /info.  Firmware version is an extra attribute.
    Unavailable on devices that never emit rssi (e.g. wired locks).
    """

    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi"

    @property
    def unique_id(self) -> str:
        return f"{self._data.get('device_id')}-rssi"

    @property
    def name(self) -> str:
        return f"{self._device_name} RSSI"

    @property
    def native_value(self) -> Optional[int]:
        return self._data.get("rssi")

    @property
    def available(self) -> bool:
        # Unavailable until the first MQTT /info with an rssi field arrives.
        return "rssi" in self._data

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {}
        if "fw_version" in self._data:
            attrs["firmware_version"] = self._data["fw_version"]
        return attrs


class TinxyIPSensor(_TinxyDiagnosticSensor):
    """Local IP address diagnostic sensor.

    Shows the device's current LAN IP from MQTT /info.
    Unavailable on devices that never emit ip (e.g. wired locks).
    """

    _attr_icon = "mdi:ip-network"

    @property
    def unique_id(self) -> str:
        return f"{self._data.get('device_id')}-ip"

    @property
    def name(self) -> str:
        return f"{self._device_name} IP Address"

    @property
    def native_value(self) -> Optional[str]:
        return self._data.get("ip")

    @property
    def available(self) -> bool:
        # Unavailable until the first MQTT /info with an ip field arrives.
        return "ip" in self._data
