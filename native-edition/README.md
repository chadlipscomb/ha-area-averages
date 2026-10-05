# Area Averages: native edition

The same averages using only stock Home Assistant: built-in helpers you could
create by hand under **Settings → Devices & Services → Helpers**, and a
dashboard of core cards. No custom integration, no template sensors, no HACS
cards.

## What it is

Each area gets one **Group → Sensor** helper with type `mean`. That's the
whole calculation. Two `input_boolean` helpers drive the dashboard's buttons.

```sh
export HA_URL=https://homeassistant.local:8123 HA_TOKEN=...
python3 native-edition/setup.py                  # dry run, prints the plan
python3 native-edition/setup.py --apply          # create or update the helpers
python3 native-edition/setup.py --apply --push   # and the dashboard
```

`setup.py` updates existing groups instead of creating duplicates. You don't
need it: everything it does can be done in the UI. It saves clicking through
the same dialog for every area, and it filters the entity list the same way
the integration does.

If the integration is also installed, `python3 native-edition/compare.py`
checks that both produce the same means.

### Turn on "Ignore non-numeric"

The Group helper's `ignore_non_numeric` option is off by default. With it off,
the group goes `unavailable` as soon as any one member does, which defeats the
point. The option isn't in the creation dialog either: create the group, then
open it again with **Configure** to find it. `setup.py` always sets it.

### Exclude the groups from other averages

The group sensors are temperature sensors in an area, so the integration and
the YAML edition would pick them up and average an average. `setup.py` adds
the `exclude-average` label to each one. Do the same if you create them by
hand.

## What it can't do

The native version gives you the average but not the health reporting. A
Group sensor publishes five attributes (`device_class`, `entity_id`,
`friendly_name`, `state_class`, `unit_of_measurement`) and there's no way to
add more.

| Missing | Why |
|---|---|
| `issue` / `issues` | Nothing built in turns "which sensors are unhealthy" into text |
| `summary`, `reporting`, `member_count` | The group doesn't count what it used |
| `offline`, `stale` | There's no built-in idea of staleness |
| `low_battery` | Nothing links a sensor to its device's battery |
| `healthy` | Depends on the above |
| `minimum` / `maximum` / `spread` | Needs three more Group helpers per area (`min`, `max`, `range`) |
| `trend` | The built-in Trend helper is a per-entity binary sensor |

**Membership is a fixed list.** The Group helper takes explicit entities, so
nothing is found by area. Move a sensor into a room and you need to re-run
`setup.py --apply` or edit the helper. The same goes for the
`exclude-average` label and for diagnostic sensors: both are applied when
`setup.py` runs.

## Dashboard

Core cards only (`sections`, `grid`, `heading`, `gauge`, `tile`,
`history-graph`), with no `custom:` cards and no Jinja.

Each area has a heading, a gauge for the average, and **Sensors** and
**Offline** buttons (tiles that toggle the `input_boolean`s). The member tiles
sit behind those buttons using core `visibility` conditions. The Offline
button only appears while that room has a sensor offline, and that check runs
live.

The gauge is the only core card that shows a large number, and its
`severity` setting gives green, yellow and red at 68, 78 and 84. It only takes
three thresholds, and it can't be restyled without card-mod, so this
dashboard uses the plain HA look.

A second view has one `history-graph` of every average.

## Comparison

| | Integration | YAML edition | Native |
|---|---|---|---|
| Finds sensors by area | yes, in about 2 s | yes, within a minute | no |
| Keeps working with most sensors offline | yes | yes | yes |
| `issue` / battery / staleness | yes | yes | no |
| Min / max / spread / trend | yes | min / max / spread | no |
| Needs custom components | yes | no | no |
| Needs HACS cards | dashboard only | dashboard only | no |
| Needs re-running | never | for new areas | for any sensor change |

Native suits a setup you want to hand to anyone, fully visible in the UI and
with nothing third party. It won't tell you why a room's average is running
on two of its nine sensors.
