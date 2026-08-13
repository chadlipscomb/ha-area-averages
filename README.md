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
Integration `0.2.0`, strategy `VERSION 2.0.0`.

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
{"strategy": {"type": "custom:area-temperatures", "history_hours": 24}}
```

`www/area_averages_strategy.js` regenerates everything on each load — 13 views:

- **Rooms** — a Status card (a real `[!WARNING]` / `[!CAUTION]` alert listing
  offline, stale and battery rows, or one quiet line when all is well), then
  three groups: **Inside**, **Outdoors**, **Equipment & Cold**. One hero card
  per room: name, area icon, temperature on the 65/68/78/84 ladder with a red
  `mdi:fire` badge above 84, a secondary that states its own coverage
  (`83.7 °F · 2 of 9 sensors · 80.6–85.8`), and a 24 h sparkline underneath.
  No member tiles at all. Fridge is judged on its own 45/50 ladder; 3D Printer,
  Bench, Outside and Weather carry no comfort semantics.
- **Trends** — core `statistics-graph` only: one 7-day overview of all room
  averages, then min/mean/max per room.
- **11 subviews** at `room-<area_id>` — members (offline ones hidden behind the
  reveal toggle), excluded members under their own subtitle, and history.

**Colour is never computed in JS.** `generate()` runs once per page load, so a
JS-computed colour would freeze until reload. The mushroom path emits a Jinja
ladder; the core path emits one tile per band with `numeric_state` visibility
conditions. Both are generated from a single `BANDS` table.

**Card degradation**: `mushroom-template-card`, `mini-graph-card` and
`stack-in-card` are probed at load via `customElements.whenDefined` with a
1500 ms timeout. If they are missing the dashboard falls back to core cards,
still correctly coloured and complete.

### Density

The overview is deliberately quiet. Eleven full-width cards each carrying a
threshold-coloured 56 px sparkline read as a wall of long red lines — the
colour ladder tops out at 84 °F and this house sits at 80–85 °F, so nearly
every line landed in the red end at once and the colour stopped meaning
anything. So by default:

- heroes are **half-width** (`columns: 6`), tiling two-up instead of stacking
  eleven full-bleed rows,
- the overview carries **no graphs at all** — history lives on each room's own
  view and on Trends,
- the hero secondary is **value + coverage** only (`82.6 °F · 4 of 6 sensors`);
  the min–max range and trend arrow would truncate at half width.

Turn any of it back on per taste:

| Option | Default | Effect |
|---|---|---|
| `sparklines` | `false` | `true` puts the 56 px mini-graph back under each hero |
| `sparkline_thresholds` | `false` | `true` restores the 65/68/78/84 colour ladder on those sparklines; otherwise they are one quiet line |
| `hero_columns` | `6` | 12 = full width, 4 = three-up, 3 = four-up |
| `detail` | `"compact"` | `"full"` adds ` · 77.9–84.9 · ↓ falling` to the secondary |

Other options: `history_hours` (24), `stats_days` (7), `cards`
(`auto` | `mushroom` | `native`), `probe_timeout_ms` (1500), `dashboard_path`,
and `areas` — `{"<area_id>": {"group": "inside|outdoors|equipment", "band": "default|fridge|none"}}`
to override the built-in grouping.

**Do not "take control"** of the dashboard in the UI editor — that freezes the
current snapshot into a static config and the strategy stops running.

## Layout

- `custom_components/area_averages/`
  - `calc.py` — pure-stdlib, **zero** homeassistant imports: member
    classification, summary text, stats, trend, issue rows, unique-id set
    arithmetic, unit normalisation. Shared by `sensor.py` and the fixture
    builder so the two can never drift; unit-testable with plain `python3 -c`.
  - `sensor.py` — `AreaAveragesManager` (registry watching, membership, orphan
    pruning) + `AreaAverageSensor` (mean, health model, trend, restore state)
  - `switch.py`, `diagnostics.py`, `icons.json`, `config_flow.py`
- `www/area_averages_strategy.js` — the dashboard strategy (see above)
- `deploy.sh` — tar → base64 → `the guest agent`, ships `custom_components/`
  **and** `www/`. Restart HA for integration changes; the JS needs none.
- `tools/`
  - `make_fixture.py` — rebuilds `fixture.json` from live HA (REST + a
    stdlib websocket client for the area/device/entity registries). If the
    deployed integration does not yet publish a contract attribute it is
    backfilled via `calc.py` and **the backfill is printed as a warning**;
    `--no-backfill` turns that off, which is how you prove a deploy took.
  - `test_strategy.mjs` — runs the real strategy under node against the
    fixture with a `customElements` shim. Four modes (mushroom/native ×
    contract/legacy) plus card-combination and toggle-scenario suites;
    ~4376 assertions, exit 1 on failure. `--json` also writes
    `strategy_output.json`.
  - `render_check.mjs` — POSTs every Jinja template the strategy emits to
    `/api/template` and asserts each one compiles. `test_strategy.mjs` only
    substring-matches those templates, so an unbalanced `{% endif %}` in a
    colour ladder passes every structural assertion and then renders an
    error string on every hero card. Since the mushroom path is the live
    path here, that is most of the dashboard's colour and secondary-text
    logic. Covers all three card modes; `--show` prints each result.
  - `gen_dashboard.py --push` — registers the resource at
    `/local/area_averages_strategy.js?v=<content-hash>` (cache-busting) and
    saves the strategy config. No flag prints the static emergency snapshot;
    `--push-static` pushes it. Needs `websockets`.
  - `ha_ws.py` — generic HA websocket one-shot (JSON list in, results out).

### Normal change loop

```bash
./deploy.sh                                    # ship both trees
# restart HA only if the Python changed
python3 tools/make_fixture.py --no-backfill    # must backfill nothing
node tools/test_strategy.mjs                   # must exit 0
node tools/render_check.mjs                    # must exit 0 (needs live HA)
python3 tools/gen_dashboard.py --push          # only if the JS changed
```

Wait for `state: RUNNING` after a restart, not just HTTP 200 — `/api/` answers
from the *outgoing* process for a few seconds, so a naive poll races and the
next websocket call gets connection-refused:

```bash
until [ "$(curl -sk -H "Authorization: Bearer $(cat ~/.config/ha-token)" https://homeassistant.local:8123/api/config | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')" = "RUNNING" ]; do sleep 5; done
```

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
