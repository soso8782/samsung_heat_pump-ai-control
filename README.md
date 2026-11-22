<p align="center"><img src="assets/logo-transparent.png" width="120" alt="Logo"></p>

# Samsung Heat Pump AI Control – smart control for Samsung EHS heat pumps

[![English](https://img.shields.io/badge/lang-English-blue.svg)](README.md) [![Slovenčina](https://img.shields.io/badge/jazyk-Sloven%C4%8Dina-green.svg)](README.sk.md)
[![Testy](https://github.com/soso8782/samsung_heat_pump-ai-control/actions/workflows/testy.yml/badge.svg)](https://github.com/soso8782/samsung_heat_pump-ai-control/actions/workflows/testy.yml)

[![Donate with PayPal](https://img.shields.io/badge/Donate-PayPal-00457C?logo=paypal&logoColor=white)](https://www.paypal.com/paypalme/sonics007) [![Support on Ko-fi](https://img.shields.io/badge/Ko--fi-Support-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/soso8782)
If this project saves you money or time, you can buy me a coffee via PayPal or Ko-fi. Thank you! ☕

[![Version](https://img.shields.io/github/v/tag/soso8782/samsung_heat_pump-ai-control?label=version&sort=semver)](https://github.com/soso8782/samsung_heat_pump-ai-control/tags)
![Python](https://img.shields.io/badge/python-3.11%2B%20(stdlib)-blue)

**Current version: 2.8.0** (16 Nov 2026) – see [CHANGELOG.md](CHANGELOG.md) for the list of changes.

A home system that collects data from a **Samsung EHS** heat pump (via the NASA bus and ESPHome), calculates actual energy
consumption and COP, monitors room temperatures and uses a language model (via [OpenRouter](https://openrouter.ai)) to
**advise on or directly adjust the heating curve** (weather compensation curve) within safe limits. Everything runs on a small
Linux container (LXC) using only the Python 3 standard library – no pip packages.

The web UI, notifications and the AI agent are available in **Slovak and English**. The web UI language is switched with the
SK/EN switch in the top bar; the language of notifications and of the agent is set with `UI_LANG` in `config.env` or in the
web UI under Settings → "Language of notifications and the agent". *Slovak documentation: [README.sk.md](README.sk.md).*

## Features

- **Real-time overview** – water temperature (flow/return, ΔT), compressor, defrost, 3-way valve, DHW, error codes.
- **Rooms** – temperatures from Home Assistant (Zigbee/ESPHome sensors), minimum target temperatures, room detail with a chart,
  open windows/doors.
- **Reference room** – click a room to choose the room and sensor the ESP regulates by (firmware select "Riadiaca izba");
  switch history in the room chart, warning when the ESP and the profile disagree, alert when the reference sensor stops
  reporting, and a sensor comparison (room temperature spread, deviation from target, cycling, kWh/day, COP). When the
  sensor drops out, the firmware switches to the other sensor in the same room.
- **Radiator balancing** – a diary of valve changes (VK 33) evaluated by room temperature spread after 24 and 48 h.
- **Weekly heating summary** – every Monday morning: room temperatures and spread, outdoor, consumption and cost, COP,
  compressor starts, DHW, agent interventions and a short AI recommendation (e.g. on balancing the radiator valves).
- **How it heats by outdoor temperature** – heating curve settings (FSV 2011/2012/2021/2022/2031/2032/4013, curve offset, gain),
  FSV change history, a chart and table of the actual water temperature at a given outdoor temperature (only minutes when the
  unit is really heating the radiators), and the min/max room temperatures by outdoor temperature.
- **Consumption and cost** – daily consumption of the whole house (from the electricity meter / distributor portal) and of the
  heat pump, cost estimate based on the tariff (DD5, high/low tariff, monthly fee, targeted energy aid), monthly summaries,
  billing period.
- **Accurate COP calculation** – heat output from flow rate and ΔT (flow rate calibrated against the manufacturer's tables),
  power input calibrated against the house's night-time consumption; daily/weekly/monthly/yearly COP.
- **Home Assistant integration** – sensors via MQTT discovery (power input, consumption, heat, COP, periods), external statistics
  for the Energy dashboard (house consumption, cost, heat pump). A separate integration (entities, agent control) lives in its
  own repository, see [Home Assistant integration](#home-assistant-integration).
- **AI agent** – evaluates the state, the weather forecast and the history every hour (or on an event); in *Advise only* mode it
  just makes recommendations, in *Control* mode it changes the allowed settings. Daily "learning" from previous days, messages
  to the agent, weekly summaries, A/B test.
- **DHW** – scheduler and manual boost of domestic hot water.
- **Notifications** – Web Push (PWA on iPhone/Android) and an HA webhook: heat pump error, backup heater, window open while
  heating, credit, …
- **Login** – administrator password, optionally Cloudflare Tunnel + Access (JWT verification), admin/user roles.
- **Statistics** – hourly database, compressor cycles, faults, comparisons, export.
- **Maintenance** – `update.sh` (safe update from git with rollback on failure), ESP firmware version in the web UI, CI tests on GitHub.

## Architecture

```
Samsung EHS ──NASA bus──> ESPHome (M5Stack Atom, m5stack-tc) ──REST/SSE──> collector.py ──> tc.db (minútové vzorky)
                                                                                   │
Home Assistant (izby, kontakty, elektromer) <──WS/REST/MQTT──> agent.py / stats.py ─┴─> stats.db (hodinové), agent.db
                                                                   │
                                          OpenRouter (LLM) <───────┤
                                                                   └──> app.py + ui.html  (web/PWA, port 80)
```

(Diagram labels: minútové vzorky = one-minute samples, izby = rooms, kontakty = contacts, elektromer = electricity meter,
hodinové = hourly.)

| File | Purpose |
|---|---|
| `collector.py` | collector from the ESP (SSE + REST, every minute) into `tc.db` |
| `agent.py` | hourly agent run, safety limits, learning, notifications, MQTT publishing |
| `app.py`, `ui.html`, `sw.js` | web server (http.server) and single-page app / PWA |
| `stats.py`, `stats_ui.js/css`, `stats_backfill.py` | hourly statistics, COP, calibration, history import from HA |
| `spotreba.py`, `ha_cost.py` | daily consumption and cost, tariffs, import into HA statistics |
| `vonku.py` | "How it heats by outdoor temperature" – curve, water and rooms by outdoor temperature |
| `ha_pub.py` | sensors for Home Assistant via MQTT discovery |
| `ha_ws.py` | minimal WebSocket client for Home Assistant |
| `contacts.py`, `contacts_mqtt.py`, `temps.py` | window/door contacts and temperature sensors |
| `dhw.py` | domestic hot water (scheduler, manual boost) |
| `interventions.py`, `abtest.py` | interventions (valves…) with evaluation, A/B test of settings |
| `notify.py`, `webpush.py` | notifications (HA webhook, Web Push with VAPID) |
| `cftunnel.py`, `cfjwt.py` | Cloudflare Tunnel and Cloudflare Access JWT verification |
| `locales/sk.json`, `locales/en.json` | web UI texts (Slovak, English); a new language = a new file with the same keys |
| `profil.py`, `profil.example.json`, `modely/` | installation profile (device, entities, rooms, limits) and the model library |
| `esphome/`, `docs/` | sample ESPHome configuration and profile documentation |
| `monitor/` | the original simple monitor (port 8099) |
| `SPEC*.md` | design notes for individual features |
| `test_*.py`, `t_*.py` | tests (unittest) |

## Requirements

- Samsung EHS heat pump (tested: Split gen. J, AE120JXEDGH + hydro unit AE160JNYDGH, 2018, R410A).
- An ESPHome bridge on the NASA bus, e.g. an M5Stack Atom with the
  [`Beormund/esphome-samsung-nasa`](https://github.com/Beormund/esphome-samsung-nasa) component and `web_server` (REST + SSE).
  The entity names the code expects are in the profile (`profil.json`, defaults in `profil.py`); a sample YAML is in `esphome/samsung-ehs.yaml`.
- Linux (a Debian 12/13 LXC is enough), Python 3.11+, systemd. No additional packages.
- Optional: Home Assistant (rooms, contacts, electricity meter, Energy dashboard), an MQTT broker, an OpenRouter key, a Cloudflare account.

## Installation

```bash
git clone https://github.com/soso8782/samsung_heat_pump-ai-control.git /opt/kurenie
cd /opt/kurenie
cp config.env.example config.env && chmod 600 config.env
nano config.env          # ESP_URL, LAT/LON, optionally HA_URL, NOTIFY_URL …
sh install.sh            # creates and starts the systemd services kurenie-collector/-web/-agent/-monitor
```

Then open `http://<container-ip>/`, set the administrator password and, in **Settings**, enter the OpenRouter key, the
Home Assistant token, MQTT and so on. These secrets are stored only locally in `config.env` (chmod 600) and are never shown again.

Daily database backup: `systemd/kurenie-backup.service` + `.timer` (runs `backup.sh`). Sample units are in `systemd/`.

### Update

Run as root in the install directory (`/opt/kurenie`). `update.sh` refuses to run if tracked files are modified, does a fast-forward
only `git merge` of the upstream (or `--ref <tag/branch>`), checks Python syntax, restarts the four services, verifies them and
rolls back to the previous commit on failure. Local files (`config.env`, databases, `backup/`) are not tracked and are not touched.

```bash
./update.sh --dry-run    # show incoming commits and diff stat, change nothing
./update.sh              # update, or --ref v2.12.0 for a specific tag
```

Tests:

```bash
python3 -m unittest test_cop test_spotreba test_vonku test_mqtt
```

## Home Assistant integration

The Home Assistant integration (a device with entities for monitoring and for controlling the agent) is no longer developed in
this repository but in its own: **[samsung_heat_pump-ai-control-home-assistant-integration](https://github.com/soso8782/samsung_heat_pump-ai-control-home-assistant-integration)**.

- Install it through **HACS → Custom repositories** using the URL of that second repository (not this one).
- It requires Samsung Heat Pump AI Control app **version 2.3.0 or newer**.
- The key is created in the app: **Settings → Home Assistant integration → Create key**.

See the integration's README for the full procedure. Sensors for the Energy dashboard (consumption, COP) are still published
by this app via MQTT (`ha_pub.py`).

## MCP server for Claude

The app has a built-in **MCP server** (Model Context Protocol) at `http://<app-address>/mcp`. Through it, Claude can ask for
the current state, single days (hour by hour), daily summaries, consumption, interventions and agent runs, and can send the
agent a message. **Heat pump settings cannot be changed through MCP** – the agent judges any request within its limits.

- Create the key in the app: **Settings → MCP server for Claude → Create key** (or on the server: `python3 mcp_api.py --novy-kluc`).
- Claude Code: `claude mcp add --transport http kurenie http://<app-address>/mcp --header "Authorization: Bearer <key>"`
- Claude Desktop: in `claude_desktop_config.json` through the `mcp-remote` bridge
  (`"command": "npx", "args": ["mcp-remote", "http://<app-address>/mcp", "--allow-http", "--header", "Authorization: Bearer <key>"]`).
- Tools: `stav`, `den`, `dni`, `spotreba`, `zasahy`, `spravy`, `beh_agenta` (read-only) and `poslat_spravu`.
- Through the Cloudflare tunnel with Access login, the `/mcp` path needs an exception: in Zero Trust → Access → Applications add
  a self-hosted application with the domain `<hostname>/mcp` and a **Bypass** policy (Everyone). The rest of the site stays behind
  Access, `/mcp` is protected by the app's key. Then `https://<hostname>/mcp` works too (no `--allow-http` in mcp-remote).

## Safety and limits

- The agent may only change settings from the `ALLOWED` list in `agent.py` (e.g. FSV 2031 42–50 °C, FSV 2032 32–38 °C, FSV 4013,
  maximum positive curve offset), and only by a small step per 12–24 h, at most 2 changes per run, never during a heat pump
  error or with stale data.
- `DRY_RUN=1` – nothing is sent to the ESP or to webhooks (log only).
- The repository contains no keys, tokens or databases (`.gitignore`). `config.env`, `cf_token`, `vapid.json`, `*.db` and the
  consumption history stay on the server only.

## Customization

Installation-specific things (rooms, entity names in the ESP, heating type, agent limits, history sources from HA, device model)
have been in the **profile** (`profil.json`) rather than in the code since version 2.0 – see [docs/PROFIL.md](docs/PROFIL.md)
and the section [Other heat pump / your own house](#other-heat-pump--your-own-house). The rest:
the electricity meter IDs (`ENERGY_EXT_ID`, `ENERGY_METER_IDS` in `ha_ws.py`, or the `elektrina` field in the profile),
the `TARIFF` price lists in `ha_cost.py` (SSE/SSD tariffs in Slovakia) and the ESP address (`ESP_URL` in `config.env`).

## Other heat pump / your own house

The app can also be deployed on another Samsung EHS (Mono/Split, different capacity, different rooms, underfloor heating)
without changing the code:

1. **ESPHome bridge** – use [`esphome/samsung-ehs.yaml`](esphome/samsung-ehs.yaml) (entity names match the default profile;
   wiring and flashing are described in [`esphome/README.md`](esphome/README.md)). Some NASA message numbers have to be filled
   in for your model (the `TODO` lines).
2. **Profile** – in Settings → **Device and home** (admin only) go through 4 steps: device, ESP entities, rooms, limits.
   The wizard loads the entities from the ESP and the temperature sensors from HA and proposes a mapping. Restart the services
   after saving. You can also edit `profil.json` by hand – the template is [`profil.example.json`](profil.example.json), the
   fields are described in [docs/PROFIL.md](docs/PROFIL.md). Without `profil.json` the original installation applies
   (Samsung AE120JXEDGH, radiators, 7 rooms).
3. **Device model** – `modely/` contains the capacity and power input tables (Maximum Heating Capacity, Peak) from the
   manufacturer's documentation for 54 Samsung EHS models:

   | Series | Models (outdoor unit) |
   |---|---|
   | Split R410A (gen. J) | `AE040/060JXEDEH`, `AE090/120/140/160JXEDGH` |
   | Split R410A (series A) | `AE120/160AXEDEH/GH` |
   | Split R32 | `AE040/060RXEDEG`, `AE090RXEDEG/GG` |
   | Split R32 (DXED) | `AE125/160DXEDEG/GG` |
   | Mono R410A (gen. J) | `AE050JXYDEH`, `AE090/120/140/160JXYDEH/GH` |
   | Mono R32 | `AE050RXYDEG`, `AE080/120/160RXYDEG/GG` |
   | Mono HT Quiet R32 | `AE080/120/140BXYDEG/GG` |
   | Mono R290 | `AE050CXYDEK`, `AE080/120/160CXYDEK/GK` |
   | Mono R290 with integrated pump | `AE050CXYBEK`, `AE080/120/160CXYBEK/GK` |

   Only `AE120JXEDGH` has been verified on a real unit; the others have `"overene": false` and the exact source (document and
   URL) in the `zdroj` field. How to add another model: [docs/MODELY.md](docs/MODELY.md).
   For a model without a table, COP works if a flow rate is available from a sensor or from the configuration
   (`STATS_FLOW_LPM`); otherwise heat output and COP are not shown.
4. **Limits** – the agent's default limits adapt to the heating type (radiators / underfloor / combined), and the profile can
   only narrow them within fixed absolute limits.

Features that are not available for your installation (DHW, Home Assistant, the agent without an OpenRouter key, house
consumption, COP without a table) are hidden in the app instead of showing errors.

## Disclaimer

This project is a hobby, with no warranty. You change the heat pump settings at your own risk – start in *Advise only* mode
or with `DRY_RUN=1`.
