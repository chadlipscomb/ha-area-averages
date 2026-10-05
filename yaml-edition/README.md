# Area Averages: YAML edition

The same averages as the integration, built from stock Home Assistant
configuration: template sensors and a Jinja macro. Nothing here runs Python
inside Home Assistant, and nothing here touches the integration or its
dashboard, so the two can run side by side.

## Files

| File | What it does |
|---|---|
| `custom_templates/area_average.jinja` | One macro, `area_stats(area_id)`, that holds all the logic and returns JSON |
| `packages/area_averages_yaml.yaml` | One trigger-based template sensor per area, plus the two reveal `input_boolean`s. Generated; the copy here is an example |
| `generate.py` | Writes both files from your registries. `--push` also assigns the sensors to their areas and builds the dashboard |
| `compare.py` | Checks every sensor against its integration counterpart |

Sensors are named `sensor.<area>_yaml_average_temperature` and publish the
same attributes as the integration: `summary`, `healthy`, `member_count`,
`reporting`, `minimum`, `maximum`, `spread`, `issue`, `issues`, `sensors`,
`excluded`, `offline`, `stale` and `low_battery`.

## Running it

```sh
export HA_URL=https://homeassistant.local:8123 HA_TOKEN=...
python3 yaml-edition/generate.py --push
```

Then copy `packages/` and `custom_templates/` into your config directory
(packages must be
[enabled](https://www.home-assistant.io/docs/configuration/packages/)), check
the configuration, and restart. After the first restart, edits only need
**Template**, **Input boolean** and **Custom Jinja templates** reloads.

If the integration is also installed, `python3 yaml-edition/compare.py`
checks that both produce the same numbers. Two independent implementations
agreeing is good evidence both are right, so it's worth re-running after
changing either one.

## How it works

Each sensor is trigger-based. Its `action:` block resolves the whole area
into one `data` variable, which the state and all fourteen attributes read.
A state-based template sensor would re-run the membership scan once per
attribute.

The triggers are the member sensors known when the file was generated, a
one-minute tick that picks up sensors added since, and Home Assistant start.

Both this edition and the integration create temperature sensors that sit in
an area, so each could end up averaging the other's averages. The macro skips
any entity that publishes `member_count`, and the generator does the same for
the trigger lists.

## Limits compared with the integration

**New areas need a re-run of `generate.py`.** Membership within an area is
live, because `area_entities()` runs on every trigger. YAML can't create a
sensor for an area it was never told about.

**Jinja can't see `entity_category`.** Templates have no access to it, so
diagnostic sensors are written into a `SKIP` list at generation time. This
matters: Zigbee devices often report an internal temperature that would
otherwise be averaged into the room. Re-run `generate.py` when a new
diagnostic sensor appears.

**The stale clock resets on restart.** The only timestamp available is
`last_reported`, which resets when Home Assistant restarts, so a dead sensor's
24-hour window starts over. Offline detection isn't affected.

**No trend.** `trend` and `trend_rate` need history between evaluations, and
there's nowhere to keep it without an integration.

**Areas have to be assigned.** YAML template sensors are created without an
area. `generate.py --push` assigns them; the integration does it on its own.

## Dashboard

`generate.py --push` creates `/yaml-temperatures`, a stored dashboard with
the same layout and glass styling as the integration's. Member lists are
[auto-entities](https://github.com/thomasloven/lovelace-auto-entities) cards
filtered by area and device class, so they stay current without a custom
strategy.

It needs three HACS cards: Mushroom (`mushroom-template-card`),
`auto-entities` and `card-mod`. A stored dashboard can't check for missing
cards the way the strategy does, so without them you'll see "Custom element
doesn't exist".

## When to use it

Use the integration if you can. It maintains itself, reacts to registry
changes immediately, keeps staleness across restarts and has no generation
step. This edition is for setups where custom components aren't an option, or
where you want everything reviewable as plain config and don't mind re-running
the generator when your areas change.
