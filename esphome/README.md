# ESPHome bridge for Samsung EHS (NASA bus)

[![English](https://img.shields.io/badge/lang-English-blue.svg)](README.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](README.sk.md)

The Samsung Heat Pump AI Control app does not talk to the heat pump directly. It reads it through a small ESP32 module running ESPHome
([`Beormund/esphome-samsung-nasa`](https://github.com/Beormund/esphome-samsung-nasa)), which is connected to the
**NASA** data bus (terminals **F1/F2**) and exposes the entities over the network through `web_server` (REST + SSE).

**Recommended starting point:** [`m5stack-tc.yaml`](m5stack-tc.yaml) is the **verified, actually running configuration** of the
reference installation (Split AE120JXEDGH, gen. J) – including curve offset control based on room temperature, a time schedule
and hybrid mode. Do **not** edit it: all site-specific values (Home Assistant entities of the room sensors, IP address) are `substitutions` at the top, overridden from a local wrapper (see below); passwords are in `secrets.yaml`.

The file [`samsung-ehs.yaml`](samsung-ehs.yaml) is a simplified **draft** (unverified) – a sample configuration whose **entity names match
the app's default profile** (`profil.default()["esp"]`). If you use it without renaming the entities, you only need to confirm the
suggestion in the "Device and home" wizard.

> The file has not been compiled for your model. The NASA message numbers marked "verified" come from the component's example;
> the `TODO` lines have to be filled in for your model (see below). A missing entity is not a problem – a feature that needs it is disabled.

## What you will need

- An M5Stack Atom (Lite/Matrix) and an **Atomic RS485 Base** (or another ESP32 board + a TTL–RS485 converter).
- 5 V power (from `V1`/`V2` on the unit's control board through a converter, or a separate USB adapter).
- A computer with ESPHome (`pip install esphome` or the ESPHome add-on in Home Assistant) and a USB cable for the first flash.

## Wiring

The NASA bus is RS485 (9600 baud, even parity). On the indoor unit's control board (MIM):

| RS485 base | terminal on the board |
|---|---|
| A (yellow) | F1 |
| B (green) | F2 |
| V+ (red) | V1 |
| G (black) | V2 |

- F3/F4 are intended for wired controllers – do not connect anything there.
- On the original board, `V1`/`V2` are usually grouped at the top right. Measure the voltage before connecting the module.
- Switch off the unit's circuit breaker while wiring. Any work on the control board is at your own risk.
- The UART pins are variables in the YAML: `uart_tx: GPIO19`, `uart_rx: GPIO22` (valid for the Atomic RS485 Base). Change them for a different board.

## Flashing

1. Copy `samsung-ehs.yaml` to your ESPHome folder and create `secrets.yaml` next to it:

   ```yaml
   wifi_ssid: "moja-wifi"
   wifi_password: "..."
   ap_password: "..."
   api_key: "<32 bajtov base64, vygeneruje ESPHome>"
   ota_password: "..."
   web_user: "kurenie"
   web_password: "..."
   ```

2. First flash over USB: `esphome run samsung-ehs.yaml`. Later updates go over Wi‑Fi (OTA).
3. Find the module's IP address (reserve it in your router) and set `ESP_URL=http://<ip>` in the app's `config.env`.
   If you enable `web_server.auth`, the app needs to know the credentials – or leave `auth` out of the YAML and keep the module
   in the home network only.
4. Open `http://<ip>/` – the table should show the entities with Slovak names.

## Local wrapper (updating without manual diffs)

Keep `m5stack-tc.yaml` as an unmodified base and put your own values in a small wrapper next to it:

1. Copy `m5stack-tc.local.example.yaml` to `m5stack-tc.local.yaml` (next to `m5stack-tc.yaml` and `secrets.yaml`) and set your `substitutions` (`esp_ip`, `ha_izba_jedalen`, `ha_izba_obyvacka`, ... - the names are listed at the top of `m5stack-tc.yaml`). Only list the ones that differ from the defaults.
2. Compile and flash the **wrapper**: `esphome run m5stack-tc.local.yaml`. Substitutions in the main (wrapper) file take precedence over those in the included package.
3. To update the firmware just replace `m5stack-tc.yaml` with the new version from the repository and flash again; your local file stays untouched. Check the release notes / the `substitutions` block for newly added names.

`!secret` values are resolved from the `secrets.yaml` in the wrapper's folder.

## Filling in the message numbers (TODO lines)

The NASA message number depends on the model and firmware. Procedure:

1. In `samsung_nasa:` set `debug_log_undefined_messages: true` and in `logger.logs` set `samsung_nasa: DEBUG`.
2. Watch the log (`esphome logs samsung-ehs.yaml`) and change a value on the unit (e.g. start a defrost, change the target) to see
   which message changes.
3. Assign the number to the commented-out entity (do not change its name) and uncomment it. After verification, set logging back to `ERROR`.

Optional entities that stay commented out in the YAML should be left empty in the profile (the wizard does this itself if it
does not find them in the ESP list).

## Required entities for the app

`vonku` (outdoor), `tw2` (flow), `tw1` (return), `freq` (compressor frequency), `wl` (target water temperature), `posun` (curve offset),
`fsv2031`, `fsv2032`. Without `freq`/`wl`/`posun` some charts are not shown and the agent has nothing to judge the curve by – add them.

## Rooms

Room temperatures (the `esp` field of the rooms in the profile) can come from any source the ESP exposes (Zigbee, BLE, entities
from Home Assistant via `platform: homeassistant`). Alternative: leave `esp` empty in the profile and use only HA sensors
(the `ha` field) – the room then has no live value from the ESP, but the history and statistics from HA work.

**Reference room.** The “Riadiaca izba” select in `m5stack-tc.yaml` decides which sensor the room control follows; the web switches it when an admin clicks a room in the Rooms card. The app pairs an option with a room by name – “<room name>” or “<room name> (<sensor>)” when the room has several sensors. When adding a room/sensor, add the option to the select and a branch to the `riadiaca_nastav` script (same order), and give the sensor an `id` and `on_value` like the others.
