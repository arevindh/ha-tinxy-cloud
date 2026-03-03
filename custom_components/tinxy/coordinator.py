"""
Coordinator for Tinxy Home Assistant integration.

Initial device state is loaded once from the REST API (so entities have a valid
starting state before the first MQTT message arrives).  After that, all state
updates are pushed in real-time by TinxyMQTTClient via async_update_from_mqtt().
No periodic polling is performed.
"""


import logging
from typing import Any, Dict
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .tinxycloud import TinxyAuthenticationException, TinxyException

# Module-level logger
_LOGGER = logging.getLogger(__name__)

API_TIMEOUT_SECONDS: int = 15


class TinxyUpdateCoordinator(DataUpdateCoordinator):
    """
    Coordinates device state for Home Assistant entities.

    * First refresh – fetches state once from the REST API so entities can render
      immediately at startup (before the MQTT broker delivers retained messages).
    * Ongoing updates – driven entirely by MQTT pushes; no polling timer is set.
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

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> Dict[str, dict]:
        """
        Fetch initial device state from the REST API (called once on startup).

        Returns:
            Dict mapping composite relay IDs to merged device + state dicts.

        Raises:
            ConfigEntryAuthFailed: On authentication errors.
            UpdateFailed: On any other API error.
        """
        try:
            async with async_timeout.timeout(API_TIMEOUT_SECONDS):
                status_result: dict = await self.api_client.get_all_status()
                status_by_id: Dict[str, dict] = {}

                for device in self.all_devices:
                    relay_id = device.get("id")
                    if relay_id in status_result:
                        status_by_id[relay_id] = {**device, **status_result[relay_id]}
                    else:
                        _LOGGER.debug("No initial status for device: %s", relay_id)
                        status_by_id[relay_id] = {**device}

                _LOGGER.info(
                    "Tinxy: loaded initial state for %d relays via REST.", len(status_by_id)
                )
                return status_by_id

        except TinxyAuthenticationException as auth_err:
            _LOGGER.error("Tinxy: authentication failed: %s", auth_err)
            raise ConfigEntryAuthFailed from auth_err
        except TinxyException as api_err:
            _LOGGER.error("Tinxy: REST API error during initial fetch: %s", api_err)
            raise UpdateFailed(f"Error communicating with API: {api_err}") from api_err
        except Exception as exc:
            _LOGGER.exception("Tinxy: unexpected error during initial fetch: %s", exc)
            raise UpdateFailed(f"Unexpected error: {exc}") from exc

    # ------------------------------------------------------------------
    # MQTT push interface
    # ------------------------------------------------------------------

    def async_update_from_mqtt(self, relay_id: str, state_dict: Dict[str, Any]) -> None:
        """
        Merge MQTT state into the coordinator's data store and notify listeners.

        Called by TinxyMQTTClient from within the HA event loop whenever a
        /{device_id}/info message arrives.

        Args:
            relay_id:   Composite ID, e.g. "aabbcc112233ddeeff445566-2".
            state_dict: At minimum {"state": bool}; may include {"brightness": int}.
        """
        if self.data is None:
            # Called before the initial REST fetch completes – ignore.
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
