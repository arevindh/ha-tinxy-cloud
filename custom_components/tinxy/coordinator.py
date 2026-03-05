"""
Coordinator for Tinxy Home Assistant integration.

All device state is delivered via MQTT push only. At startup every relay is
initialised with safe defaults; the broker's retained /info messages (buffered
before coordinator.data is ready) are replayed on top immediately after,
giving entities their correct last-known state.
"""


import logging
from typing import Any, Dict, List, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .tinxycloud import TinxyException

# Module-level logger
_LOGGER = logging.getLogger(__name__)


class TinxyUpdateCoordinator(DataUpdateCoordinator):
    """
    Coordinates device state for Home Assistant entities.

    * First refresh - builds the state dict from the device list with safe defaults.
    * Retained MQTT /info messages that arrive before the first refresh completes
      are buffered and replayed on top of the defaults.
    * Ongoing updates - driven entirely by MQTT pushes; no polling timer is set.
    """

    def __init__(self, hass: HomeAssistant, api_client: Any) -> None:
        """
        Initialize the coordinator.

        Args:
            hass: The HomeAssistant instance.
            api_client: TinxyCloud REST client instance.
        """
        super().__init__(
            hass,
            _LOGGER,
            name="Tinxy",
            update_interval=None,   # MQTT push-driven – no polling
        )
        self.hass: HomeAssistant = hass
        self.api_client: Any = api_client
        # Static device list populated after sync_devices(); treated as immutable.
        self.all_devices: list[dict] = self.api_client.list_all_devices()
        # Buffer for MQTT messages that arrive before coordinator.data is ready.
        self._mqtt_buffer: List[Tuple[str, Dict[str, Any]]] = []

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> Dict[str, dict]:
        """
        Build initial state from the device list with safe defaults.
        Replays any buffered MQTT retained messages on top so the very first
        state shown to the user reflects the broker's last-known device state.

        Returns:
            Dict mapping composite relay IDs to device + state dicts.

        Raises:
            UpdateFailed: If the device list is unexpectedly empty.
        """
        try:
            status_by_id: Dict[str, dict] = {}

            for device in self.all_devices:
                relay_id = device.get("id")
                if relay_id:
                    status_by_id[relay_id] = {
                        **device,
                        "state": 0,
                        "status": 0,
                        "brightness": 0,
                    }

            if not status_by_id:
                raise UpdateFailed("No devices found - check API key and device setup.")

            # Replay buffered MQTT retained messages over the defaults.
            if self._mqtt_buffer:
                _LOGGER.debug(
                    "Tinxy: replaying %d buffered MQTT messages.", len(self._mqtt_buffer)
                )
                for relay_id, state_dict in self._mqtt_buffer:
                    if relay_id in status_by_id:
                        status_by_id[relay_id] = {**status_by_id[relay_id], **state_dict}
                self._mqtt_buffer.clear()

            _LOGGER.info(
                "Tinxy: initialised state for %d relays (MQTT retained messages will follow).",
                len(status_by_id),
            )
            return status_by_id

        except UpdateFailed:
            raise
        except TinxyException as exc:
            raise UpdateFailed(f"Tinxy error during setup: {exc}") from exc
        except Exception as exc:
            _LOGGER.exception("Tinxy: unexpected error during setup: %s", exc)
            raise UpdateFailed(f"Unexpected error: {exc}") from exc

    # ------------------------------------------------------------------
    # MQTT push interface
    # ------------------------------------------------------------------

    def async_update_from_mqtt(self, relay_id: str, state_dict: Dict[str, Any]) -> None:
        """
        Merge MQTT state into the coordinator's data store and notify listeners.

        If coordinator.data is not yet populated (first refresh still in progress),
        the update is buffered and will be replayed once it completes.

        Args:
            relay_id:   Composite ID, e.g. "aabbcc112233ddeeff445566-2".
            state_dict: At minimum {"state": bool}; may include {"brightness": int}.
        """
        if self.data is None:
            # First refresh not yet complete – buffer for replay.
            self._mqtt_buffer.append((relay_id, state_dict))
            _LOGGER.debug("Tinxy MQTT: buffered update for %s (coordinator not ready)", relay_id)
            return

        if relay_id not in self.data:
            _LOGGER.debug("Tinxy MQTT: received update for unknown relay %s", relay_id)
            return

        # Build updated data dict (shallow copy so DataUpdateCoordinator detects change)
        new_data = dict(self.data)
        new_data[relay_id] = {**self.data[relay_id], **state_dict}

        # async_set_updated_data notifies all subscribed CoordinatorEntity instances
        self.async_set_updated_data(new_data)
        _LOGGER.debug("Tinxy MQTT state applied: %s → %s", relay_id, state_dict)
