# ha-area-averages

Custom Home Assistant integration (`area_averages`) that maintains one robust
**"Area Average"** sensor per HA Area, plus a **custom dashboard strategy** that
regenerates the whole "Area Temperatures" dashboard from live state on every
page load.

The integration is pure HA core APIs — no HACS, no external Python deps
(`requirements: []`). The *dashboard* may use already-installed HACS cards, but
every card it emits has a core-only equivalent and it degrades to a complete,
correctly coloured core-only dashboard if those cards are missing.

Config entry created 2026-08-12.
Integration `0.3.0`, strategy `VERSION 3.0.0`.

## Three implementations, one calculation

| | `custom_components/` | `yaml-edition/` | `native-edition/` |
|---|---|---|---|
| Built from | a custom integration | template sensors + a Jinja macro | built-in Group helpers |
| Dashboard | custom strategy (JS) | stored config + auto-entities | **core cards only** |
| Auto-discovers by Area | yes, ~2 s | yes, ~1 min | no — static list |
| `issue` / battery / staleness | yes | yes | **no** |
| Needs custom components | yes | no | no |
| Needs HACS cards | dashboard only | dashboard only | **no** |
| Lives at | `/room-temperatures` | `/yaml-temperatures` | `/native-temperatures` |

All three run side by side and agree on every mean. Each edition has a
`compare.py` that checks itself against the integration — two independent
implementations of one calculation are worth having precisely because they can
be diffed. Run them after changing anything.

The rest of this file describes the integration; each edition has its own
README.

## The health model (read this first)

Every member of an average lands in **exactly one** of three buckets, and the
invariant `len(offline) + len(stale) + reporting == member_count` always holds:

| Bucket | Meaning | In the mean? |
|---|---|---|
| **offline** | No state object, `unavailable`/`unknown`, non-numeric, or a unit that cannot be converted into the average's unit. No time window — offline the instant it goes dark. | no |
| **stale** | Holds a convertible number, but its last actual report is older than `stale_hours`. | **no** (dropped since 0.2.0) |
| **reporting** | Value is actually inside the mean. | yes |

A member that has *never* produced a number is **offline**, not stale — there is
nothing to wait for. Last-report times are persisted via restore-state, so an HA
restart does not reset the staleness clock.

**State** = mean of the reporting members, in the HA system unit (°F here);
mixed °C/°F members are converted first. `unknown` only when nothing reports.

> Note: `reporting` used to include stale members, and `issue` used to omit
> offline ones. Both changed in 0.2.0. Names and shapes are unchanged, but the
> values moved and the Status card got much louder — that is the point, it was
> previously claiming health it did not have.

## Attributes

| Attribute | Shape | Meaning |
|---|---|---|
| `offline` | `list[str]` | Members contributing nothing right now (see above). |
| `stale` | `list[str]` | Numeric but older than `stale_hours`; excluded from the mean. |
| `reporting` | `int` | Members actually inside the mean. |
| `member_count` | `int` | `len(sensors)`. |
| `summary` | `str` | One phrase for a tile secondary: `"9 sensors"`, `"2 of 9 sensors"`, `"1 sensor"`, `"no sensors reporting"`. Never null. |
| `healthy` | `bool` | `reporting > 0 and not issues`. A room with nothing reporting is never healthy. |
| `issue` / `issues` | `str \| null` / `list[str]` | Offline rows, then stale, then battery: `"<Name>: offline"`, `"<Name>: no data for N h"`, `"<Name>: battery N%"`, `"<Name>: low battery"`. `<Name>` falls back to the entity_id when the friendly name is generic ("Temperature") or under 4 chars. `issue` is the `"; "`-joined string. |
| `minimum` / `maximum` | `float \| null` | Smallest / largest value in the mean, average's unit, 2 dp. Null when nothing reports. |
| `spread` | `float \| null` | `maximum - minimum`. Null when fewer than 2 report. |
| `trend` | `str` | `rising` / `falling` / `steady` / `unknown`. In-memory only, never from the recorder — so it is honestly `unknown` for ~15 min after every restart. |
| `trend_rate` | `float \| null` | Signed native units per hour, 2 dp. Null while `unknown`. |
| `sensors` / `excluded` | `list[str]` | Members / entities carrying the exclude label. |
| `low_battery` | `list[str]` | Battery entities under the threshold. |

`sensors`, `excluded` and `offline` are excluded from the recorder.

## Membership

Everything in the entity registry with the configured `device_class` whose
entity (or device) sits in the area. Diagnostic/config-category entities (Zigbee
LQI temps, radio chip temps, …) and disabled entities are never included.
Membership refreshes live on any entity/device/area/label registry change — no
restart needed.

**Exclusions**: put the `exclude-average` label on an entity *or its device* and
it leaves the average within seconds, listed in `excluded`. If the configured
label matches nothing, the integration now logs a **warning** instead of
silently excluding nothing.

Each average is auto-assigned to its area, so it appears on area cards and to
voice assistants.

## Entities the integration owns

- `sensor.<area>_average_<device_class>` — one per area per configured class.
- `switch.area_averages_show_offline` — the dashboard's reveal control
  (`RestoreEntity`, category CONFIG, default off). The integration owns it so a
  fresh install is not missing a hand-made helper. The old
  `input_boolean.show_offline_sensors` still works as a fallback and can be
  deleted.

Turning a device class **off** in options now removes its averages from the
entity registry instead of orphaning them. An average that comes back later with
the same `unique_id` reclaims its entity_id and history.

## Options (Settings → Devices & Services → Area Averages → Configure)

| Option | Default | Notes |
|---|---|---|
| Sensor types to average | `temperature` | multi-select: humidity, pressure, CO2, illuminance, PM2.5 |
| Stale after | 24 h | measured from the last *numeric* report |
| Low battery threshold | 20 % | |
| Trend window | 120 min | 15–720; bounds the in-memory sample deque |
| Exclude label | `exclude-average` | rendered as a **label picker**, so it cannot be a typo |

Every supported device class has a fixed unit (°F/%/psi/ppm/lx/µg/m³) — a
MEASUREMENT sensor is never created unitless, which would make HA discard its
long-term statistics when a unit appeared later.

## The dashboard

`/room-temperatures` ("Area Temperatures"). The saved lovelace config is only:

```json
{"strategy": {"type": "custom:area-temperatures"}}
```

`www/area_averages_strategy.js` regenerates everything on each load — two views.

**Rooms** is a status card, then **one section per area**. Each section is:

| | |
|---|---|
| heading | area name + its registry icon |
| reading | the temperature at 34 px, on the 65/68/78/84 ladder with a red `mdi:fire` badge above 84. Small line underneath states coverage: `4 of 6 sensors` |
| `[N sensors]` | reveals every member of that average |
| `[N offline]` | reveals only the dark ones; present only while that room has one |
| members | tiles, hidden until a button is pressed |
| excluded | greyed `mdi:sigma-lower` tiles, behind the sensors button |

The temperature is the **primary** line, not the secondary — it is the reason
the page exists, so nothing else competes with it for size. Fridge is judged on
its own 45/50 ladder; 3D Printer, Bench, Outside and Weather carry no comfort
semantics at all.

**Trends** is core `statistics-graph` only: a 7-day overview of every room
average, then min/mean/max per room. All history lives here — the overview
carries no graphs, because eleven of them made the temperatures unreadable.

### Liquid Glass

Every card is a translucent, blurred pane lifted off the page by a soft shadow
and edged with a specular highlight along its top rim
(`inset 0 1px 0`) — the cue that makes glass read as glass rather than as a
flat translucent rectangle. Readings sit at 22 px radius, buttons are full
pills, member tiles are tighter at 16 px, and each pane lifts 1 px on hover.

It is defined entirely in `color-mix()` against HA's own theme variables, so
one definition tracks light and dark without hardcoding a palette — the pane
borrows `--card-background-color` and thins it, and takes its rim from
`--primary-text-color`. The harness asserts no six-digit hex ever reaches the
CSS.

Glass needs **card-mod**, which is probed at load like any other custom card.
Without it every card renders as an ordinary HA card, same structure, same
colours — `glass: false` does the same thing deliberately, and the harness
asserts that turning it off changes styling *only*, never structure.

### Options

| Option | Default | Effect |
|---|---|---|
| `glass` | `true` | `false` for flat HA cards (also the automatic result when card-mod is missing) |
| `detail` | `"compact"` | `"full"` adds ` · 77.9–84.9 · ↓` to the small line |
| `cards` | `"auto"` | `"native"` forces core cards, `"mushroom"` skips the probe |
| `stats_days` | `7` | Trends window |
| `probe_timeout_ms` | `1500` | how long to wait for custom cards to define |
| `areas` | `{}` | `{"<area_id>": {"group": "inside\|outdoors\|equipment", "band": "default\|fridge\|none"}}` |

**Do not "take control"** of the dashboard in the UI editor — that freezes the
strategy into a static snapshot. `gen_dashboard.py --push` restores it.

## Reveal controls are never gated on generate-time state

`generate()` runs **once per page load**. Anything decided from a snapshot of
`hass.states` at that moment freezes until the next reload, so the reveal tile
and its heading badges are emitted for *every* room that has members and gate
themselves with live `visibility` conditions instead. Gating their emission on
"does this room have an offline member right now" produced a dead end: load the
page while a room is healthy, let a member die, and its tile hid itself with no
control anywhere to bring it back. Kitchen hit this on every restart — its MQTT
sensor is non-retained and comes back `unknown`.

The same rule governs the per-room offline badges on the Rooms view: each badge
watches only its **own** room's members. A single "worst room" badge with a
dashboard-wide visibility OR keeps naming the room it picked at generate time
while pointing at a different room's outage.

## Notes / quirks

- HA is at `https://homeassistant.local:8123` (LAN IP; the external hostname 401s from
  LAN), token at `~/.config/ha-token`. Always `curl -sk`.
- Members that are offline across two consecutive restarts can carry a
  `last_numeric` stamp from a restart instant (13 do today, all at 03:05–03:09
  on 2026-08-13). A pre-0.2.1 seeding bug wrote them and restore-state persisted
  them. Harmless — `offline` beats `stale` in classification, so the value is
  never read, and it is overwritten the moment the sensor reports. Deliberately
  **not** cleaned up on restore: dropping stamps for currently-non-numeric
  members would also drop them during the restart window when healthy sensors
  are briefly unavailable, destroying the staleness clocks restore-state exists
  to preserve.
- `deploy.sh` sets `COPYFILE_DISABLE=1`; without it macOS tar writes
  AppleDouble `._sensor.py` junk into the HA config dir on every deploy.
- The config flow creates the entry with no questions, so it can be added
  headlessly: `POST /api/config/config_entries/flow {"handler":"area_averages"}`.
- Non-retained MQTT members read `unknown` for a minute or two after every HA
  restart, so their room briefly shows "no sensors reporting". It self-heals on
  the next publish; this is honest, not a bug.
- The label registry stores `exclude-average` as label_id `exclude_average`;
  both spellings resolve.
- **Known double-counting**: `sensor.derry_temperature` /
  `sensor.lr_plant_derry_temperature` are the same plant, as are the two Otter
  and two Hippocrates sensors. Label one of each pair `exclude-average`.
  Deliberately not solved in code — reliable detection needs value-series
  correlation, and de-duplicating by `device_id` is provably wrong here
  (`esp32_thermal` ambient vs object share a device and measure different
  things).
- The Weather average still mixes dew point and feels-like with real temps;
  those are a labelling decision, not a code one. It is at least out of the
  comfort-banded group now.
- `sensor.altai_*_target_temperature` (setpoints), the three
  `sensor.sensorbox_*_temperature` (documented 5–8 °F high) and
  `sensor.fridge_t` (fridge cabinet ≠ Kitchen room air) carry the label.
- `/dashboard-temperatures` is the owner's hand-curated dashboard — a separate
  artifact, never touched by these tools.
