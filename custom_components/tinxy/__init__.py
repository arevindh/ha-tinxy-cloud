"""
Tinxy Home Assistant Integration
This module sets up and manages the Tinxy integration for Home Assistant.
"""

from __future__ import annotations
from typing import Any

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, TINXY_BACKEND, CONF_MQTT_USERNAME, CONF_MQTT_PASSWORD
from .tinxycloud import TinxyCloud, TinxyHostConfiguration, TinxyMQTTCredentials
from .coordinator import TinxyUpdateCoordinator
from .mqtt_client import TinxyMQTTClient

# Logger for this module
LOGGER = logging.getLogger(__name__)

# Supported platforms for Tinxy devices
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.LIGHT,
    Platform.FAN,
    Platform.LOCK,
    Platform.SENSOR,
]

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry
) -> bool:
    """
    Set up Tinxy integration from a config entry.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        entry (ConfigEntry): The configuration entry for Tinxy.

    Returns:
        bool: True if setup was successful, False otherwise.
    """
    LOGGER.info("Setting up Tinxy integration for entry_id=%s", entry.entry_id)

    # Ensure domain data exists
    hass.data.setdefault(DOMAIN, {})

    # Create web session for REST API communication
    web_session = async_get_clientsession(hass)

    # Prepare host configuration
    host_config = TinxyHostConfiguration(
        api_token=entry.data[CONF_API_KEY],
        api_url=TINXY_BACKEND,
    )

    # Initialize REST API client and fetch device list (done once at startup)
    api = TinxyCloud(host_config=host_config, web_session=web_session)
    try:
        await api.sync_devices()
        LOGGER.info("Successfully synced %d Tinxy devices.", len(api.devices))
    except Exception as exc:
        LOGGER.error("Failed to sync Tinxy devices: %s", exc)
        return False

    # Create coordinator (MQTT push-driven; performs one REST fetch on first_refresh)
    coordinator = TinxyUpdateCoordinator(hass, api)

    # Collect unique physical device IDs (strip the "-relay_no" suffix)
    unique_device_ids: list[str] = list(
        {d["device_id"] for d in api.list_all_devices()}
    )

    # Create MQTT client wired to coordinator's push-update method
    # Credentials are cached in entry.data to avoid an API call on every startup.
    # The fetcher uses the cache on the first call and force-refreshes on auth failures.
    _cred_fetched_once = False

    async def _get_mqtt_credentials() -> TinxyMQTTCredentials:
        nonlocal _cred_fetched_once
        if not _cred_fetched_once and CONF_MQTT_USERNAME in entry.data:
            _cred_fetched_once = True
            LOGGER.debug("Tinxy: using cached MQTT credentials.")
            return TinxyMQTTCredentials(
                username=entry.data[CONF_MQTT_USERNAME],
                password=entry.data[CONF_MQTT_PASSWORD],
            )
        # Fetch fresh credentials from the API (first run or after auth failure)
        creds = await api.async_get_mqtt_credentials()
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_MQTT_USERNAME: creds.username,
                CONF_MQTT_PASSWORD: creds.password,
            },
        )
        _cred_fetched_once = True
        LOGGER.debug("Tinxy: MQTT credentials fetched and cached.")
        return creds

    mqtt_client = TinxyMQTTClient(
        hass=hass,
        on_state_update=coordinator.async_update_from_mqtt,
        credentials_fetcher=_get_mqtt_credentials,
    )
    # Give the REST client a reference so set_device_state() uses MQTT
    api.mqtt_client = mqtt_client

    # Start MQTT connection in the background
    await mqtt_client.async_start(unique_device_ids)
    LOGGER.info(
        "Tinxy MQTT client started for %d physical devices.", len(unique_device_ids)
    )

    # Store references for platforms and unload
    hass.data[DOMAIN][entry.entry_id] = (api, coordinator, mqtt_client)

    # Forward setup to supported platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    LOGGER.info("Tinxy integration setup complete for entry_id=%s", entry.entry_id)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry
) -> bool:
    """
    Unload a Tinxy config entry and clean up resources.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        entry (ConfigEntry): The configuration entry to unload.

    Returns:
        bool: True if unload was successful, False otherwise.
    """
    LOGGER.info("Unloading Tinxy integration for entry_id=%s", entry.entry_id)

    # Stop MQTT client before unloading platforms
    entry_data = hass.data[DOMAIN].get(entry.entry_id)
    if entry_data and len(entry_data) >= 3:
        mqtt_client: TinxyMQTTClient = entry_data[2]
        await mqtt_client.async_stop()
        LOGGER.info("Tinxy MQTT client stopped.")

    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        LOGGER.info("Tinxy integration unloaded for entry_id=%s", entry.entry_id)
    else:
        LOGGER.warning("Failed to unload Tinxy integration for entry_id=%s", entry.entry_id)
    return unload_ok
