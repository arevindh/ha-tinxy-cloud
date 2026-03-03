"""
Tinxy MQTT Client

Manages persistent MQTT connection to the Tinxy broker for real-time device state
updates (subscribe) and device control commands (publish).

Protocol notes derived from broker observation:
  Subscribe : /{device_id}/#
  Status msg : /{device_id}/info  → {"rssi":…,"status":1,"state":"0011","bright":"100100100100"}
                  state[i]         → relay (i+1) on/off  ('1'=on, '0'=off)
                  bright[i*3:(i+1)*3] → brightness % for relay (i+1), zero-padded to 3 chars
  Command msg: /{device_id}       ← publish {"n": relay_no, "on": "1"|"0"}
                                  ← publish {"n": relay_no, "bright": value}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import aiomqtt

from homeassistant.core import HomeAssistant
from .tinxycloud import TinxyMQTTCredentials

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY   = 5   # seconds – network drop reconnect delay
MAX_AUTH_RETRIES  = 3   # max consecutive credential-refresh attempts


class TinxyMQTTClient:
    """
    Lightweight MQTT client for the Tinxy broker.

    Subscribes to every known device topic, parses /info payloads into
    per-relay state dicts, and forwards them to a caller-supplied callback.

    Also exposes async_publish_command() for sending toggle / brightness commands.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        on_state_update: Callable[[str, Dict[str, Any]], None],
        credentials_fetcher: Callable[[], Awaitable[TinxyMQTTCredentials]],
    ) -> None:
        """
        Initialise the client (does NOT connect yet).

        Args:
            hass:                HomeAssistant instance.
            on_state_update:     Callback fired with (relay_id, state_dict) on every
                                 /info message.
            credentials_fetcher: Async callable that returns fresh TinxyMQTTCredentials.
                                 Called once at startup and again whenever the broker
                                 rejects the connection (e.g. after a password reset).
        """
        self._hass = hass
        self._on_state_update = on_state_update
        self._credentials_fetcher = credentials_fetcher
        self._credentials: Optional[TinxyMQTTCredentials] = None
        self._device_ids: Set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[aiomqtt.Client] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_start(self, device_ids: list[str]) -> None:
        """Fetch credentials, then start the MQTT background loop."""
        self._device_ids = set(device_ids)
        # Fetch credentials eagerly so a bad API key fails at setup time.
        self._credentials = await self._credentials_fetcher()
        self._task = self._hass.async_create_background_task(
            self._run_loop(), "tinxy_mqtt_loop"
        )
        _LOGGER.info(
            "Tinxy MQTT client started for %d devices on %s:%d",
            len(device_ids), self._credentials.broker, self._credentials.port,
        )

    async def async_stop(self) -> None:
        """Cancel the background loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._client = None
        _LOGGER.debug("Tinxy MQTT client stopped.")

    async def async_publish_command(
        self,
        device_id: str,
        relay_no: int,
        state: str,
        brightness: Optional[int] = None,
    ) -> None:
        """
        Publish a toggle or brightness command to a device.

        Args:
            device_id:  The Tinxy device _id (not the relay composite id).
            relay_no:   1-based relay number.
            state:      "ON" or "OFF" (used when brightness is None).
            brightness: 0-100 percentage; when provided, sends a brightness command
                        instead of a simple toggle.
        """
        if self._client is None:
            _LOGGER.warning(
                "Tinxy MQTT: cannot publish command for %s – not connected.", device_id
            )
            return

        if brightness is not None:
            payload = json.dumps({"n": relay_no, "bright": brightness, "from": "HA"})
        else:
            payload = json.dumps({"n": relay_no, "on": "1" if state == "ON" else "0", "from": "HA"})

        topic = f"/{device_id}"
        try:
            await self._client.publish(topic, payload, qos=0)
            _LOGGER.debug("MQTT publish → %s : %s", topic, payload)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Tinxy MQTT: failed to publish command: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Persistent MQTT loop with automatic reconnection and credential refresh."""
        auth_retries = 0

        while True:
            try:
                creds = self._credentials
                async with aiomqtt.Client(
                    hostname=creds.broker,
                    port=creds.port,
                    username=creds.username,
                    password=creds.password,
                    identifier=f"ha-tinxy-{creds.username}",
                ) as client:
                    self._client = client
                    auth_retries = 0  # reset on successful connect
                    _LOGGER.info(
                        "Tinxy MQTT connected to %s as %s",
                        creds.broker, creds.username,
                    )

                    for device_id in self._device_ids:
                        await client.subscribe(f"/{device_id}/#", qos=0)
                        _LOGGER.debug("Subscribed to /%s/#", device_id)

                    async for message in client.messages:
                        self._dispatch_message(str(message.topic), message.payload)

            except aiomqtt.MqttConnectError as exc:
                # Broker rejected the connection – likely stale credentials
                # (e.g. user reset their password).
                self._client = None
                auth_retries += 1
                if auth_retries > MAX_AUTH_RETRIES:
                    _LOGGER.error(
                        "Tinxy MQTT: broker rejected credentials %d times – giving up. "
                        "Reload the integration after fixing the account password.",
                        auth_retries,
                    )
                    return
                _LOGGER.warning(
                    "Tinxy MQTT: broker rejected connection (%s) – refreshing credentials "
                    "(attempt %d/%d).",
                    exc, auth_retries, MAX_AUTH_RETRIES,
                )
                try:
                    self._credentials = await self._credentials_fetcher()
                except Exception as fetch_exc:  # noqa: BLE001
                    _LOGGER.error(
                        "Tinxy MQTT: failed to refresh credentials: %s – retrying in %ds",
                        fetch_exc, RECONNECT_DELAY,
                    )
                    await asyncio.sleep(RECONNECT_DELAY)
                # Reconnect immediately with fresh credentials (no extra sleep).

            except aiomqtt.MqttError as exc:
                self._client = None
                _LOGGER.warning(
                    "Tinxy MQTT connection lost: %s – reconnecting in %ds",
                    exc, RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)
            except asyncio.CancelledError:
                self._client = None
                _LOGGER.debug("Tinxy MQTT loop cancelled.")
                return
            except Exception as exc:  # noqa: BLE001
                self._client = None
                _LOGGER.error(
                    "Tinxy MQTT unexpected error: %s – reconnecting in %ds",
                    exc, RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)

    def _dispatch_message(self, topic: str, payload: bytes) -> None:
        """Route an incoming MQTT message to the appropriate handler."""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError, TypeError):
            _LOGGER.debug("Tinxy MQTT: ignoring non-JSON payload on %s", topic)
            return

        # Topic format: /{device_id} or /{device_id}/subtopic
        parts = topic.strip("/").split("/")
        if not parts:
            return

        device_id = parts[0]
        subtopic   = parts[1] if len(parts) > 1 else None

        try:
            if subtopic == "info":
                self._handle_info(device_id, data)
            elif subtopic is None:
                # Root topic /{device_id} – command-echo from the broker.
                # Arrives ~200 ms before the /info confirmation; use it for
                # an immediate optimistic state update.
                self._handle_command_echo(device_id, data)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Tinxy MQTT: error handling message on %s: %s", topic, exc
            )

    def _handle_command_echo(
        self, device_id: str, data: Dict[str, Any]
    ) -> None:
        """
        Handle a command-echo on /{device_id} (no subtopic).

        Payload shapes observed:
          {"n": 2, "on": "1"}        – relay toggle (on = "1" | "0")
          {"n": 1, "bright": 66}     – brightness change

        Fires an immediate optimistic callback.  The authoritative /info
        message that follows will overwrite it with the confirmed state.
        """
        relay_no = data.get("n")
        if relay_no is None:
            return

        relay_id   = f"{device_id}-{relay_no}"
        state_dict: Dict[str, Any] = {}

        if "on" in data:
            state_dict["state"] = str(data["on"]) == "1"
        if "bright" in data:
            try:
                state_dict["brightness"] = int(data["bright"])
            except (ValueError, TypeError):
                pass

        if not state_dict:
            return

        _LOGGER.debug("MQTT command-echo for %s: %s", relay_id, state_dict)
        try:
            self._on_state_update(relay_id, state_dict)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error(
                "Tinxy MQTT: error in command-echo callback for %s: %s", relay_id, exc
            )

    def _handle_info(self, device_id: str, data: Dict[str, Any]) -> None:
        """
        Parse a /{device_id}/info message and fire per-relay callbacks.

        state  : string where state[i] == '1' → relay (i+1) is ON
        bright : string where bright[i*3:(i+1)*3] gives brightness% for relay (i+1)
                 values are zero-padded to 3 chars, e.g. "066100100100"
        """
        # Defensively cast to str – some firmware versions send these as numbers
        state_str  = str(data.get("state",  ""))
        bright_str = str(data.get("bright", ""))

        for i, char in enumerate(state_str):
            relay_no = i + 1
            relay_id = f"{device_id}-{relay_no}"

            state_dict: Dict[str, Any] = {"state": char == "1"}

            # Parse brightness for this relay if available
            b_start = i * 3
            b_end   = b_start + 3
            if len(bright_str) >= b_end:
                try:
                    state_dict["brightness"] = int(bright_str[b_start:b_end])
                except ValueError:
                    pass

            # Forward device-level diagnostics on every relay update so the
            # RSSI sensor (keyed on relay -1) can read them from coordinator data.
            if "rssi" in data:
                state_dict["rssi"] = data["rssi"]
            if "ip" in data:
                state_dict["ip"] = data["ip"]
            if "version" in data:
                state_dict["fw_version"] = data["version"]

            try:
                self._on_state_update(relay_id, state_dict)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error(
                    "Tinxy MQTT: error in state-update callback for %s: %s", relay_id, exc
                )
