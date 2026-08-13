# Area Averages — the all-native edition

The same idea using **nothing but stock Home Assistant**: built-in helpers you
could create by clicking through Settings → Devices & Services → Helpers, and a
dashboard of core Lovelace cards. No custom integration, no template sensors,
no custom Jinja, no HACS cards.

Live at **`/native-temperatures`** ("Area Temps (Native)"), alongside the
integration and the YAML twin. All three agree on the mean:

```
area                  native  integration   delta
3d_printer              83.6         83.6    0.00
bedroom                 77.9         77.9    0.00
bench                  75.29        75.29    0.00     ← 2 of 9 sensors alive
fridge                 49.82        49.82    0.00
kitchen                78.44        78.44    0.00
kittens                77.36        77.36    0.00
living_room            78.08        78.08    0.00
master_bathroom        78.44        78.44    0.00
office                78.914        78.91    0.00
outside                74.66        74.66    0.00
weather              70.5425        70.54    0.00
```

```bash
python3 native-edition/compare.py
```

## What it is

Per area, one **Group → Sensor** helper, `type: mean`. That is the entire
calculation. Plus two `input_boolean` helpers for the dashboard's buttons.

```bash
python3 native-edition/setup.py                  # dry run — prints the plan
python3 native-edition/setup.py --apply          # create/update the helpers
python3 native-edition/setup.py --apply --push   # ...and the dashboard
```

`setup.py` is idempotent — it reconciles existing groups rather than making
duplicates — and it is a *convenience*, not a requirement. Everything it does
is clicking in the UI; it exists because doing it eleven times by hand is
tedious, and because the entity list has to be filtered the same way the
integration filters it.

### `ignore_non_numeric` is the whole ballgame

The Group helper defaults this to **off**, and with it off the group goes
`unavailable` the moment *any* member dies — the exact opposite of your
requirement. It is also **not in the creation dialog**: you have to create the
group, then reopen it via Configure to find the toggle. `setup.py` always sets
it, which is most of why it exists.

With it on, Bench averages 75.29 from 2 live sensors out of 9. Requirement met,
natively.

### These groups need the exclude label

The group sensors are themselves temperature sensors sitting in an area, so
both the integration and the YAML twin would otherwise pick them up as
members and average an average. `setup.py` puts `exclude-average` on each one.
Worth knowing if you build a third of these by hand.

## What native cannot do

This is the honest ledger, and it is the real answer to "can you do the same
thing natively?" — **the average, yes; the health reporting, no.**

A Group sensor publishes exactly five attributes: `device_class`, `entity_id`,
`friendly_name`, `state_class`, `unit_of_measurement`. There is no hook to add
a sixth. So all of this is simply gone:

| Lost | Why |
|---|---|
| `issue` / `issues` | Nothing native aggregates "which sensors are unhealthy" into text |
| `summary`, `reporting`, `member_count` | The group does not count what it used |
| `offline`, `stale` | No native staleness concept at all |
| `low_battery` | Nothing correlates a sensor with its device's battery |
| `healthy` | Composite of the above |
| `minimum` / `maximum` / `spread` | Would need three more Group helpers per area (`min`, `max`, `range`) — 44 helpers instead of 11 |
| `trend` | The built-in Trend helper is binary (rising/falling) and per-entity |

**Membership is a static list.** This is the biggest one. The Group helper
takes explicit entities, so nothing is discovered by Area. Move a sensor into
a room and the group does not notice — re-run `setup.py --apply`, or edit the
helper in the UI. The integration re-resolves from the registry in about two
seconds; the YAML twin re-resolves within a minute.

**Exclusions are applied once, at build time.** Adding the `exclude-average`
label to a sensor does nothing here until you re-run `setup.py`.

**Diagnostic sensors are filtered at build time too**, for the same reason.

## The dashboard

Core cards only — verified: the saved config contains no `custom:` card types
and no Jinja at all. Card types used: `sections`, `grid`, `heading`, `gauge`,
`tile`, `history-graph`.

Per area: a heading, a **gauge** for the average, a **Sensors** button and an
**Offline** button (both `tile` cards toggling the `input_boolean`s), then the
member tiles gated behind those buttons with core `visibility` conditions. The
Offline button only appears while that room actually has a dark member —
evaluated live, not baked in.

The gauge is the native answer to "make the temperature readable": it is the
only core card that shows a large number, and its `severity` thresholds give
green/yellow/red at 68/78/84. It cannot do the five-band ladder — core gauge
severity takes exactly three cuts — and there is no way to restyle it without
card-mod, so this dashboard is plain HA, not Liquid Glass.

Second view: one core `history-graph` of all eleven averages.

## Which of the three to use

| | integration | YAML twin | native |
|---|---|---|---|
| Auto-discovers by Area | yes, ~2 s | yes, ~1 min | **no** |
| Survives all-but-one offline | yes | yes | yes |
| `issue` / battery / staleness | yes | yes | **no** |
| Min / max / spread / trend | yes | min/max/spread | **no** |
| Needs custom components | yes | no | no |
| Needs HACS cards | dashboard only | dashboard only | **no** |
| Needs regeneration | never | new areas | **any sensor change** |

Native is the right answer if you want something you can hand to anyone, fully
inspectable in the UI, with no third-party anything. It gives you the robust
per-room average and a clean dashboard. It does not give you the part that made
the original request interesting — knowing *why* a room's average is only
running on two of nine sensors.
