"""
Tinxy MQTT Client

Manages persistent MQTT connection to the Tinxy broker for real-time device state
updates (subscribe) and device control commands (publish).

Uses paho-mqtt (HA's own MQTT library) directly — no extra dependencies.
paho handles keepalive at the socket level, so silently broken connections
(e.g. firewall DROP) are detected reliably via on_disconnect(rc != 0).

Protocol notes derived from broker observation:
  Subscribe : /{device_id}/#
  Status msg : /{device_id}/info  - {"rssi":...,"status":1,"state":"0011","bright":"100100100100"}
                  state[i]         - relay (i+1) on/off  ('1'=on, '0'=off)
                  bright[i*3:(i+1)*3] - brightness % for relay (i+1), zero-padded to 3 chars
  Command msg: /{device_id}       <- publish {"n": relay_no, "on": "1"|"0"}
                                  <- publish {"n": relay_no, "bright": value}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import paho.mqtt.client as mqtt

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from .tinxycloud import TinxyMQTTCredentials

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY  = 5   # seconds - network drop reconnect delay
MAX_AUTH_RETRIES = 3   # max consecutive credential-refresh attempts
CONNECT_TIMEOUT  = 30  # seconds - abort if broker unreachable (firewall DROP)
KEEPALIVE        = 30  # seconds - paho sends PINGREQ every N seconds;
                       #           on_disconnect fires if PINGRESP is not received

# CONNACK return codes that indicate a credential problem
_AUTH_CONNECT_CODES = {
    4,  # Connection Refused - bad username or password
    5,  # Connection Refused - not authorized
}


class TinxyMQTTClient:
    """
    Lightweight MQTT client for the Tinxy broker backed by paho-mqtt.

    paho runs its network loop in a background thread (loop_start/loop_stop).
    Callbacks bridge back to the HA asyncio event loop via call_soon_threadsafe
    so all HA state updates happen on the correct thread.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        on_state_update: Callable[[str, Dict[str, Any]], None],
        credentials_fetcher: Callable[[], Awaitable[TinxyMQTTCredentials]],
        entry_id: Optional[str] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
    ) -> None:
        self._hass = hass
        self._on_state_update = on_state_update
        self._credentials_fetcher = credentials_fetcher
        self._entry_id = entry_id
        self._on_disconnected = on_disconnected
        self._credentials: Optional[TinxyMQTTCredentials] = None
        self._device_ids: Set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._paho: Optional[mqtt.Client] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_start(self, device_ids: list[str]) -> None:
        """Fetch credentials, then start the MQTT background loop."""
        self._device_ids = set(device_ids)
        self._credentials = await self._credentials_fetcher()
        self._task = self._hass.async_create_background_task(
            self._run_loop(), "tinxy_mqtt_loop"
        )
        _LOGGER.info(
            "Tinxy MQTT: client started for %d device(s), broker %s:%d.",
            len(device_ids), self._credentials.broker, self._credentials.port,
        )

    async def async_stop(self) -> None:
        """Cancel the background loop and disconnect paho."""
        if self._task and not self._task.done():
            _LOGGER.debug("Tinxy MQTT: stopping - cancelling background loop.")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._paho is not None:
            try:
                self._paho.disconnect()
                self._paho.loop_stop()
            except Exception:  # noqa: BLE001
                pass
            self._paho = None
        _LOGGER.debug("Tinxy MQTT: client stopped.")

    async def async_publish_command(
        self,
        device_id: str,
        relay_no: int,
        state: str,
        brightness: Optional[int] = None,
    ) -> None:
        """Publish a toggle or brightness command to a device."""
        if self._paho is None:
            _LOGGER.error(
                "Tinxy MQTT: cannot publish for %s - not connected.", device_id
            )
            return

        if brightness is not None:
            payload = json.dumps({"n": relay_no, "bright": brightness, "by": "Home Assistant"})
        else:
            payload = json.dumps({"n": relay_no, "on": "1" if state == "ON" else "0", "by": "Home Assistant"})

        topic = f"/{device_id}"
        try:
            result = self._paho.publish(topic, payload, qos=0)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.error(
                    "Tinxy MQTT: publish to %s failed (rc=%d).", topic, result.rc
                )
            else:
                _LOGGER.debug("Tinxy MQTT: publish -> %s : %s", topic, payload)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Tinxy MQTT: failed to publish command: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_set(fut: asyncio.Future, value: Any) -> None:
        """Set a future result only if it has not been resolved yet."""
        if not fut.done():
            fut.set_result(value)

    async def _run_loop(self) -> None:  # noqa: C901
        """
        Persistent MQTT loop with automatic reconnection and credential refresh.

        Architecture:
          - A fresh paho client is created for every connection attempt.
          - paho network loop runs in a background thread (loop_start/loop_stop).
          - on_connect / on_disconnect callbacks signal asyncio Futures so the
            async loop can await connection state changes cleanly.
          - paho detects keepalive failures natively and fires on_disconnect,
            so no additional watchdog task is needed.
        """
        auth_retries = 0
        attempt = 0
        loop = asyncio.get_running_loop()

        while True:
            attempt += 1
            creds = self._credentials

            # Per-attempt Futures resolved by paho callbacks from its thread.
            connected_fut: asyncio.Future[int] = loop.create_future()
            disconnected_fut: asyncio.Future[int] = loop.create_future()

            client = mqtt.Client(
                client_id=f"ha-tinxy-{creds.username}",
                protocol=mqtt.MQTTv311,
                clean_session=True,
            )
            client.username_pw_set(creds.username, creds.password)

            def on_connect(
                c: mqtt.Client,
                userdata: Any,
                flags: dict,
                rc: int,
            ) -> None:
                loop.call_soon_threadsafe(
                    TinxyMQTTClient._safe_set, connected_fut, rc
                )

            def on_disconnect(
                c: mqtt.Client,
                userdata: Any,
                rc: int,
            ) -> None:
                # Unblock connected_fut too, in case disconnect fires before CONNACK
                loop.call_soon_threadsafe(
                    TinxyMQTTClient._safe_set, connected_fut, -1
                )
                loop.call_soon_threadsafe(
                    TinxyMQTTClient._safe_set, disconnected_fut, rc
                )

            def on_message(
                c: mqtt.Client,
                userdata: Any,
                msg: mqtt.MQTTMessage,
            ) -> None:
                loop.call_soon_threadsafe(
                    self._dispatch_message, msg.topic, msg.payload
                )

            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.on_message = on_message

            _LOGGER.debug(
                "Tinxy MQTT: connecting to %s:%d as '%s' (attempt #%d).",
                creds.broker, creds.port, creds.username, attempt,
            )

            try:
                client.connect_async(creds.broker, creds.port, keepalive=KEEPALIVE)
                client.loop_start()
                self._paho = client

                # Wait for CONNACK with timeout
                try:
                    conn_rc = await asyncio.wait_for(
                        asyncio.shield(connected_fut), timeout=CONNECT_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "Tinxy MQTT: connect to %s timed out after %ds "
                        "(broker unreachable / firewall DROP). Retrying in %ds.",
                        creds.broker, CONNECT_TIMEOUT, RECONNECT_DELAY,
                    )
                    client.loop_stop()
                    self._paho = None
                    if self._on_disconnected:
                        self._on_disconnected()
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                # CONNACK indicated refusal
                if conn_rc != 0:
                    client.loop_stop()
                    self._paho = None
                    is_auth = conn_rc in _AUTH_CONNECT_CODES
                    auth_retries += 1

                    _LOGGER.warning(
                        "Tinxy MQTT: broker at %s refused connection "
                        "(CONNACK rc=%d, %s). Attempt %d/%d.",
                        creds.broker, conn_rc,
                        "auth failure" if is_auth else "server error",
                        auth_retries, MAX_AUTH_RETRIES,
                    )

                    if auth_retries > MAX_AUTH_RETRIES:
                        raise ConfigEntryAuthFailed(
                            f"Tinxy MQTT: broker refused connection after {auth_retries} "
                            "consecutive auth failures. Please re-authenticate."
                        )

                    if is_auth:
                        try:
                            self._credentials = await self._credentials_fetcher()
                            _LOGGER.info(
                                "Tinxy MQTT: credentials refreshed - retrying connection."
                            )
                        except Exception as fetch_exc:  # noqa: BLE001
                            _LOGGER.warning(
                                "Tinxy MQTT: failed to refresh credentials: %s - "
                                "retrying in %ds.",
                                fetch_exc, RECONNECT_DELAY,
                            )
                            await asyncio.sleep(RECONNECT_DELAY)
                    else:
                        await asyncio.sleep(RECONNECT_DELAY)
                    continue

                # Successfully connected
                auth_retries = 0
                _LOGGER.info(
                    "Tinxy MQTT: connected to %s:%d as '%s'. "
                    "Subscribing to %d device(s) (keepalive=%ds).",
                    creds.broker, creds.port, creds.username,
                    len(self._device_ids), KEEPALIVE,
                )

                for device_id in self._device_ids:
                    client.subscribe(f"/{device_id}/#", qos=0)
                    _LOGGER.debug("Tinxy MQTT: subscribed to /%s/#", device_id)

                _LOGGER.debug(
                    "Tinxy MQTT: all %d subscriptions active - "
                    "paho will detect silent disconnects via keepalive.",
                    len(self._device_ids),
                )

                # Wait for disconnect.
                # paho fires on_disconnect for:
                #   rc=0  - clean DISCONNECT packet
                #   rc>0  - unexpected: TCP drop, keepalive timeout, etc.
                disc_rc = await disconnected_fut
                client.loop_stop()
                self._paho = None

                if disc_rc == 0:
                    _LOGGER.info(
                        "Tinxy MQTT: clean disconnect from %s. Reconnecting in %ds.",
                        creds.broker, RECONNECT_DELAY,
                    )
                else:
                    _LOGGER.warning(
                        "Tinxy MQTT: unexpected disconnect from %s (rc=%d - %s). "
                        "Reconnecting in %ds.",
                        creds.broker, disc_rc,
                        mqtt.error_string(disc_rc),
                        RECONNECT_DELAY,
                    )
                if self._on_disconnected:
                    self._on_disconnected()
                await asyncio.sleep(RECONNECT_DELAY)

            except asyncio.CancelledError:
                _LOGGER.debug("Tinxy MQTT: loop cancelled (integration unloading).")
                try:
                    client.disconnect()
                    client.loop_stop()
                except Exception:  # noqa: BLE001
                    pass
                self._paho = None
                return

            except Exception as exc:  # noqa: BLE001
                _LOGGER.error(
                    "Tinxy MQTT: unexpected error: %s - reconnecting in %ds.",
                    exc, RECONNECT_DELAY,
                )
                try:
                    client.loop_stop()
                except Exception:  # noqa: BLE001
                    pass
                self._paho = None
                await asyncio.sleep(RECONNECT_DELAY)

    def _dispatch_message(self, topic: str, payload: bytes) -> None:
        """Route an incoming MQTT message to the appropriate handler."""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError, TypeError):
            _LOGGER.warning("Tinxy MQTT: ignoring non-JSON payload on %s", topic)
            return

        parts = topic.strip("/").split("/")
        if not parts:
            return

        device_id = parts[0]
        subtopic   = parts[1] if len(parts) > 1 else None

        try:
            if subtopic == "info":
                self._handle_info(device_id, data)
            elif subtopic is None:
                self._handle_command_echo(device_id, data)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error(
                "Tinxy MQTT: error handling message on %s: %s", topic, exc
            )

    def _handle_command_echo(
        self, device_id: str, data: Dict[str, Any]
    ) -> None:
        """
        Handle a command-echo on /{device_id} (no subtopic).

        Payload shapes observed:
          {"n": 2, "on": "1"}    - relay toggle (on = "1" | "0")
          {"n": 1, "bright": 66} - brightness change

        Fires an immediate optimistic callback. The authoritative /info
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
            if "state" not in state_dict:
                state_dict["state"] = True

        if not state_dict:
            return

        _LOGGER.debug("Tinxy MQTT: command-echo for %s: %s", relay_id, state_dict)
        try:
            self._on_state_update(relay_id, state_dict)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error(
                "Tinxy MQTT: error in command-echo callback for %s: %s", relay_id, exc
            )

    def _handle_info(self, device_id: str, data: Dict[str, Any]) -> None:
        """
        Parse a /{device_id}/info message and fire per-relay callbacks.

        state  : string where state[i] == '1' -> relay (i+1) is ON
        bright : string where bright[i*3:(i+1)*3] gives brightness% for relay (i+1)
                 values are zero-padded to 3 chars, e.g. "066100100100"
        """
        state_str  = str(data.get("state",  ""))
        bright_str = str(data.get("bright", ""))

        for i, char in enumerate(state_str):
            relay_no = i + 1
            relay_id = f"{device_id}-{relay_no}"

            state_dict: Dict[str, Any] = {"state": char == "1"}

            if "status" in data:
                state_dict["status"] = data["status"]

            b_start = i * 3
            b_end   = b_start + 3
            if len(bright_str) >= b_end:
                try:
                    state_dict["brightness"] = int(bright_str[b_start:b_end])
                except ValueError:
                    pass

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
