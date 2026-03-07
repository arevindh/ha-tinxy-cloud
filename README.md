
# Tinxy Home Assistant Integration

> **Important Notice**  
> Users of the Tinxy cloud integration must update to version **5.0.0** before **10th March 2026**.  
> Older versions will stop working after this date. Please update via HACS or manually at the earliest.

A custom Home Assistant integration for [Tinxy.in](https://tinxy.in/) smart devices.  
Uses MQTT push for real-time state updates and control with no polling and no delays.

---

## Community

Have questions, need help, or want to share feedback? Join the Tinxy community on Discord.

**[Join Discord](https://discord.gg/cSPwkkQg)**

---

## Supported Devices

- Switches
- Lights (including dimmer)
- Fans (with speed presets: Low / Medium / High)
- Locks

---

## Installation

### HACS (Recommended)

This integration is available in the HACS default store.

1. Open **HACS** in Home Assistant and go to **Integrations**.
2. Search for **Tinxy** and click **Download**.
3. Restart Home Assistant.

### Manual

Copy the `custom_components/tinxy` folder into your Home Assistant `custom_components` directory and restart.

---

## Configuration

1. In Home Assistant, go to **Settings → Devices & Services → Add Integration**.
2. Search for **Tinxy**.
3. Enter your API key from the Tinxy mobile app and click **Submit**.

All paired Tinxy devices will be discovered and added automatically.

---

## Local Control

A local-network alternative is available at [arevindh/tinxylocal](https://github.com/arevindh/tinxylocal) for users who prefer not to use the cloud. Note that the local integration supports only a limited number of older device models. **EVA series devices are not supported** by the local integration.

---

## License

See [LICENSE](LICENSE) for details.


