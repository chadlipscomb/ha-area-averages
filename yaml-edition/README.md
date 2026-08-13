# Area Averages — the integration-free twin

The same capability as the custom integration one directory up, built entirely
out of stock Home Assistant configuration: **no `custom_components`, no Python
running inside HA**. Live at `/yaml-temperatures` ("Area Temps (YAML)"),
running side by side with the integration and agreeing with it exactly.

Nothing here modifies the integration or its dashboard.

## Parity, measured

Every generated sensor is paired against its integration counterpart and
compared on the live instance:

| | YAML | integration | Δ |
|---|---|---|---|
| 3D Printer | 86.0 | 86.0 | 0.00 |
| Bedroom | 78.17 | 78.17 | 0.00 |
| Bench | 76.64 | 76.64 | 0.00 |
| Fridge | 52.52 | 52.52 | 0.00 |
| Kitchen | 82.04 | 82.04 | 0.00 |
| Kittens | 78.71 | 78.71 | 0.00 |
| Living Room | 80.17 | 80.17 | 0.00 |
| Master Bathroom | 80.06 | 80.06 | 0.00 |
| Office | 77.11 | 77.11 | 0.00 |
| Outside | 76.28 | 76.28 | 0.00 |
| Weather | 68.25 | 68.25 | 0.00 |

`member_count`, `reporting`, `summary` and every `issue` string match on all
eleven. Two independent implementations landing on the same number is the
strongest evidence either one is right — the comparison is worth re-running
after a change to *either* side:

```bash
python3 yaml-edition/compare.py
```

## What it is

| Piece | What it does |
|---|---|
| `custom_templates/area_average.jinja` | One macro, `area_stats(area_id)`, holding all the logic. Returns JSON. |
| `packages/area_averages_yaml.yaml` | One trigger-based template sensor per area + the two reveal `input_boolean`s. |
| `generate.py` | Writes both files from the live registries; `--push` also assigns areas and builds the dashboard. |
| `deploy.sh` | Ships them to the HA VM, **validates the config, and only then reloads**. |
| `compare.py` | Checks every sensor against its integration counterpart. |

Sensors are `sensor.<area>_yaml_average_temperature`, publishing the same
contract: `summary`, `healthy`, `member_count`, `reporting`, `minimum`,
`maximum`, `spread`, `issue`, `issues`, `sensors`, `excluded`, `offline`,
`stale`, `low_battery`.

### Computed once, not fourteen times

Each sensor is **trigger-based**, and the `action:` block resolves the whole
area into a single `data` variable that state and all fourteen attributes then
read. A plain state-based template sensor would re-run the entire membership
scan once per attribute.

Triggers are the member sensors known at generation time (so it reacts
instantly), plus a one-minute tick (which is what notices sensors added since)
and `homeassistant: start`.

### Never averages an average

Both this twin and the integration publish `member_count`, and both create
temperature sensors that sit *in* an area — so each would happily swallow the
other, and itself on the next pass. The macro skips any entity that publishes
`member_count`, which is self-maintaining: a new area cannot reintroduce the
loop between regenerations. The generator applies the same rule to the trigger
lists.

## Where it is genuinely worse than the integration

These are the honest costs of dropping the integration, not bugs.

**The area list is a snapshot.** YAML cannot create an entity for an area it
was never told about. Membership *within* an area is live — `area_entities()`
is evaluated on every trigger, so a sensor moved into a room is picked up
within the minute with no regeneration — but a **new area needs `generate.py`
re-run**. The integration creates the sensor by itself.

**Jinja cannot see `entity_category`.** The integration filters diagnostic
entities at runtime; templates have no access to that field, so the 18
diagnostic temperature sensors here are baked into a `SKIP` list at
generation time. This is not cosmetic — the first prototype silently averaged
the Zigbee door/window device temperatures into the Bedroom and read 95 °F.
**Re-run `generate.py` when a new diagnostic sensor appears.**

**Staleness resets on restart.** The integration persists each member's
last-numeric timestamp through restore-state. Here the only thing available is
`last_reported`, which resets when HA restarts — so a dead sensor's 24-hour
clock restarts with it, and `stale` under-reports until the window elapses
again. Offline detection is unaffected.

**No trend.** `trend` / `trend_rate` need history between evaluations. There is
nowhere to keep it without an integration, and reading the recorder from a
template on every tick is not worth the cost.

**Entities need their area assigned.** Template YAML sensors land area-less,
which breaks `area_name()` and keeps them off area cards and away from voice
assistants. `generate.py --push` writes the registry for them; the integration
does it natively.

**It is generated.** Eleven near-identical YAML blocks are not something to
hand-maintain, which is the thing an integration exists to avoid.

## The dashboard

`/yaml-temperatures` — the same shape and the same Liquid Glass as the
integration's: one section per area, the temperature at 34 px on the
65/68/78/84 ladder, `[N sensors]` and `[N offline]` buttons, member tiles
behind them.

The member lists are **`auto-entities`** cards filtered by area and
`device_class`, so they stay live without any bespoke JavaScript — this
dashboard is plain stored config, no custom strategy. That is the one thing
here that is arguably *nicer* than the integration's approach.

Card dependencies are HACS frontend cards already installed:
`mushroom-template-card`, `auto-entities`, `card-mod`. They have no
degradation path here — unlike the strategy, a stored config cannot probe for
them at load. Without them the cards show as "Custom element doesn't exist".

## Running it

```bash
python3 yaml-edition/generate.py --push   # regenerate, assign areas, push dashboard
yaml-edition/deploy.sh                    # ship + validate + reload
python3 yaml-edition/compare.py           # prove parity with the integration
```

`deploy.sh` refuses to reload if `check_config` comes back invalid. A brand-new
package file needs one full HA restart to be merged; edits to an existing one
only need the reloads `deploy.sh` already does.

## Which one to use

The integration, if you can install one — it is self-maintaining, event-driven,
survives restarts properly, and needs no regeneration step. This edition earns
its place when custom components are off the table (a supervised install you
don't control, someone who won't run third-party Python, or wanting the whole
thing reviewable as config) and you accept re-running a generator when the
house changes shape.
