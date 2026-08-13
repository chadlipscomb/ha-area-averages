# ha-area-averages

Custom Home Assistant integration (`area_averages`) that maintains one robust
**"Area Average"** sensor per HA Area, plus a generated **Temperatures**
dashboard. Pure HA core APIs — no HACS, no external Python deps
(`requirements: []`), designed to grow to other sensor types.

Config entry created 2026-08-12.

## What each average sensor does

One `sensor.<area>_average_temperature` per area that has ≥1 temperature
sensor (e.g. `sensor.living_room_average_temperature`).

- **State** = mean of the members currently reporting a numeric state. One
  live sensor is enough; dead/unavailable members just drop out of the mean.
  `unknown` only when *nothing* reports. Mixed °C/°F members are converted
  to the HA system unit (°F).
- **Membership is automatic**: everything in the entity registry with
  `device_class: temperature` whose entity (or device) sits in the area.
  Diagnostic/config-category entities (Zigbee device temps, radio chip
  temps, ...) are never included. Disabled entities skipped. Membership
  refreshes live on any entity/device/area/label registry change — no
  restart needed.
- **Exclusions**: put the `exclude-average` label on an entity *or its
  device* and it leaves the average within seconds (it's then listed in the
  `excluded` attribute). Label name is configurable in the options flow.
- **`issue` attribute**: `null` when healthy, otherwise a `;`-joined string
  (machine-friendly list in `issues`) of:
  - stale members — no numeric report for >24 h (configurable). Last-report
    times are persisted via restore-state so HA restarts don't reset the
    staleness clock.
  - low batteries — the member's device's battery entity ≤20 %
    (configurable), or a `battery` binary_sensor that's `on`.
- Other attributes: `sensors` (members), `excluded`, `member_count`,
  `reporting`, `stale`, `low_battery`.
- Each average is auto-assigned to its area, so it shows up on area cards
  and voice assistants.

## Options (Settings → Devices & Services → Area Averages → Configure)

| Option | Default |
|---|---|
| Sensor types to average | `temperature` (multi-select: humidity, pressure, CO2, illuminance, PM2.5) |
| Stale after | 24 h |
| Low battery threshold | 20 % |
| Exclude label | `exclude-average` |

Adding `humidity` in options instantly creates `sensor.<area>_average_humidity`
for every area with humidity sensors — that's the "add features later" path.
New device classes beyond the list: extend `SUPPORTED_DEVICE_CLASSES` in
`const.py` (and `UNIT_CONVERTERS`/`NICE_NAMES` in `sensor.py` if the class
needs unit conversion).

## Layout

- `custom_components/area_averages/` — the integration
  - `sensor.py` — `AreaAveragesManager` (registry watching, membership) +
    `AreaAverageSensor` (mean, issue detection, restore-state persistence)
  - `config_flow.py` — zero-question setup + options flow
- `www/area_averages_strategy.js` — **custom dashboard strategy** (no HACS).
  The dashboard config is just `{"strategy": {"type": "custom:area-temperatures"}}`;
  the JS regenerates every view/section from live state on each dashboard
  load, so new sensors, new areas and label changes appear without any
  regeneration step. Per room: status/issues panel, average tile, member
  tiles, greyed excluded tiles. Members that are `unavailable`/`unknown`
  hide themselves (native `visibility` conditions, live) and an
  **"Offline sensors"** toggle tile appears only in rooms hiding something —
  it flips `input_boolean.show_offline_sensors` to reveal them everywhere.
  Second view: 24 h history graphs. **Do not "take control"** of the
  dashboard in the UI editor — that freezes it into a static snapshot.
- `deploy.sh` — tar → base64 → `the guest agent` → ships
  `custom_components/` **and** `www/` to `<config>/`;
  restart HA only for integration changes (JS needs none).
- `tools/gen_dashboard.py --push` — registers/refreshes the strategy
  resource (`/local/area_averages_strategy.js?v=<content-hash>`, so browser
  caches bust on change) and saves the strategy dashboard config. After
  editing the JS: `./deploy.sh` then `--push`. `--push-static` pushes the
  old generated snapshot as an emergency no-JS fallback (always-visible
  tiles, goes stale). Needs `pip install websockets`.
- `tools/ha_ws.py` — generic HA websocket one-shot (JSON list in, results out).

## Notes / quirks

- HA reached at `https://homeassistant.local:8123` (LAN IP; external hostname 401s
  from LAN), token at `~/.config/ha-token`.
- The config flow creates the entry with no questions, so it can be added
  headlessly: `POST /api/config/config_entries/flow {"handler":"area_averages"}`.
- Averages of the "Weather" area mix dew point/feels-like with real temps,
  and plant/soil sensors sit in room averages — label anything unwanted
  with `exclude-average`.
- `sensor.altai_bed_target_temperature` + `sensor.altai_nozzle_target_temperature`
  carry the label since 2026-08-12 (setpoints, not temperatures), as do the
  three `sensor.sensorbox_*_temperature` (documented 5–8 °F high) and
  `sensor.fridge_t` (fridge cabinet ≠ Kitchen room air).
