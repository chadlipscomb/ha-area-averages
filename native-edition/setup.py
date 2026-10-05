#!/usr/bin/env python3
"""Build the same thing out of stock Home Assistant, and nothing else.

No custom integration, no template sensors, no custom Jinja, no HACS cards.
Every entity here is a **built-in helper** — the kind you would create by
clicking through Settings → Devices & Services → Helpers — and the dashboard
uses only core Lovelace cards.

    python3 native-edition/setup.py            # show what it would do
    python3 native-edition/setup.py --apply    # create/update the helpers
    python3 native-edition/setup.py --apply --push   # ...and the dashboard

What it creates, per area:
  * a Group → Sensor helper, type `mean`, ignore_non_numeric ON
    (that flag is the whole ballgame: it defaults OFF, and with it off the
    group goes unavailable the moment ANY member dies — the exact opposite
    of "keep working while one sensor still reports")

and once, globally:
  * two input_boolean helpers driving the dashboard's reveal buttons

Idempotent: re-running reconciles membership instead of making duplicates.
Stdlib only; the websocket client comes from ../tools/make_fixture.py.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import ssl
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "tools"))
from make_fixture import _get as rest, ws  # noqa: E402

BASE = os.environ.get("HA_URL", "https://homeassistant.local:8123").rstrip("/")
TOKEN = os.environ.get("HA_TOKEN") or (
    pathlib.Path("~/.config/ha-token").expanduser().read_text().strip()
)

DEVICE_CLASS = "temperature"
EXCLUDE_LABEL = "exclude_average"
SUFFIX = "Native Average Temperature"

SENSORS_TOGGLE = "input_boolean.na_show_sensors"
OFFLINE_TOGGLE = "input_boolean.na_show_offline"

DASHBOARD_URL_PATH = "native-temperatures"
DASHBOARD_TITLE = "Area Temps (Native)"
DASHBOARD_ICON = "mdi:home-thermometer-outline"

# Core `gauge` severity takes exactly three ascending cuts, so the five-band
# ladder collapses to the three that matter: comfortable, warm, too hot.
GAUGE = {"min": 55, "max": 95, "green": 68, "yellow": 78, "red": 84}
FRIDGE_GAUGE = {"min": 30, "max": 60, "green": 34, "yellow": 45, "red": 50}
COLD_AREAS = {"fridge"}


def api(method: str, path: str, payload=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in str(text).lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


# ------------------------------------------------------------------ survey


def survey():
    """Which areas get a group, and which sensors belong to each.

    The native Group helper takes a **static** entity list, so every filter
    the integration applies at runtime has to be applied right here instead:
    diagnostic entities, disabled ones, the exclude-average label, and any
    entity that is itself an average.
    """
    areas, entities, devices = ws(
        [
            {"type": "config/area_registry/list"},
            {"type": "config/entity_registry/list"},
            {"type": "config/device_registry/list"},
        ]
    )
    areas = {a["area_id"]: a for a in areas}
    devices = {d["id"]: d for d in devices}
    states = {s["entity_id"]: s for s in rest("/api/states")}

    per_area: dict[str, list[str]] = {}
    excluded: dict[str, list[str]] = {}
    for entry in entities:
        entity_id = entry["entity_id"]
        if not entity_id.startswith("sensor.") or entry.get("disabled_by"):
            continue
        state = states.get(entity_id)
        if not state or state["attributes"].get("device_class") != DEVICE_CLASS:
            continue
        # Never let an average become a member of another average.
        if entry.get("platform") in ("area_averages", "group"):
            continue
        if state["attributes"].get("member_count") is not None:
            continue
        if entity_id.endswith("_yaml_average_temperature"):
            continue
        # Templates cannot read entity_category and neither can the Group
        # helper — here it is simply a filter applied at build time.
        if entry.get("entity_category"):
            continue
        device = devices.get(entry.get("device_id") or "")
        area_id = entry.get("area_id") or (device or {}).get("area_id")
        if not area_id or area_id not in areas:
            continue
        tags = set(entry.get("labels") or []) | set((device or {}).get("labels") or [])
        if EXCLUDE_LABEL in tags:
            excluded.setdefault(area_id, []).append(entity_id)
            continue
        per_area.setdefault(area_id, []).append(entity_id)

    return (
        areas,
        {a: sorted(m) for a, m in sorted(per_area.items())},
        {a: sorted(m) for a, m in excluded.items()},
    )


# ------------------------------------------------------------- the helpers


def group_entries():
    """Existing group config entries, keyed by title."""
    entries = api("GET", "/api/config/config_entries/entry?domain=group")
    return {e["title"]: e for e in entries}


def create_group(name: str, members: list[str]) -> str:
    flow = api(
        "POST",
        "/api/config/config_entries/flow",
        {"handler": "group", "show_advanced_options": True},
    )
    api(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        {"next_step_id": "sensor"},
    )
    result = api(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        {
            "name": name,
            "entities": members,
            "hide_members": False,
            "type": "mean",
        },
    )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"group creation failed for {name}: {result}")
    return result["result"]["entry_id"]


def set_group_options(entry_id: str, members: list[str]) -> None:
    """Membership + the ignore_non_numeric flag.

    Also the only way to set that flag at all: it is absent from the create
    flow and defaults to False, so a freshly created group is fragile until
    this runs.
    """
    flow = api(
        "POST",
        "/api/config/config_entries/options/flow",
        {"handler": entry_id, "show_advanced_options": True},
    )
    result = api(
        "POST",
        f"/api/config/config_entries/options/flow/{flow['flow_id']}",
        {
            "entities": members,
            "hide_members": False,
            "ignore_non_numeric": True,
            "type": "mean",
        },
    )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"group options failed for {entry_id}: {result}")


TOGGLES = [
    ("na_show_sensors", "Native dashboard — show all sensors", "mdi:format-list-bulleted"),
    ("na_show_offline", "Native dashboard — show offline sensors", "mdi:lan-disconnect"),
]


def _input_boolean_entity(registry: list[dict], unique_id: str) -> str | None:
    """The entity_id of the input_boolean whose collection id is unique_id."""
    for entry in registry:
        if entry.get("platform") == "input_boolean" and entry.get("unique_id") == unique_id:
            return entry["entity_id"]
    return None


def ensure_toggles(apply: bool) -> None:
    """Create the two reveal helpers with the entity_ids the dashboard uses.

    Two things make this fiddly, and both have bitten:

      * `input_boolean/create` takes no object_id -- HA slugifies the NAME, so
        "Native dashboard - show all sensors" lands on
        input_boolean.native_dashboard_show_all_sensors, not the id the
        dashboard references. It has to be renamed into place afterwards, or
        every entity ref on the generated dashboard points at nothing.
      * The rename moves the ENTITY id and leaves the COLLECTION id alone. So
        `input_boolean/list` keeps reporting the slugified name for ever, and
        a guard comparing that against the wanted object_id can never match.

    Deciding from `input_boolean/list` is what made this non-idempotent: the
    guard missed on every run after the first, so each one created another
    duplicate helper and then died renaming it onto a taken entity_id --
    taking `tag_entities` down with it, which is what puts the
    exclude-average label on the group sensors.

    So: decide from the entity registry, which is what the dashboard actually
    references, and find the new helper by its unique_id -- the one value
    that survives the rename.
    """
    registry = ws([{"type": "config/entity_registry/list"}])[0]
    existing = {entry["entity_id"] for entry in registry}

    for object_id, name, icon in TOGGLES:
        entity_id = f"input_boolean.{object_id}"
        if entity_id in existing:
            print(f"  toggle {entity_id} already exists")
            continue
        print(f"  toggle {entity_id} -> create")
        if not apply:
            continue

        created = ws([{"type": "input_boolean/create", "name": name, "icon": icon}])[0]
        # NOT f"input_boolean.{created['id']}": the entity_id comes from the
        # slugified name and the collection id from the same slug only while
        # that slug is free. Once it is taken the two diverge (collection
        # ..._2, entity ...) and guessing renames the wrong entity.
        actual = _input_boolean_entity(
            ws([{"type": "config/entity_registry/list"}])[0], created["id"]
        )
        if actual is None:
            raise RuntimeError(
                f"created input_boolean {created['id']!r} but it never reached "
                "the entity registry"
            )
        if actual != entity_id:
            ws([{
                "type": "config/entity_registry/update",
                "entity_id": actual,
                "new_entity_id": entity_id,
            }])
            print(f"    renamed {actual} -> {entity_id}")


def tag_entities(areas, per_area, apply: bool) -> None:
    """Give each group sensor its area, and the exclude-average label.

    The label is not decoration: these groups are temperature sensors sitting
    in an area, so without it BOTH the integration and the YAML twin would
    pick them up as members and average an average.
    """
    entities = ws([{"type": "config/entity_registry/list"}])[0]
    by_id = {e["entity_id"]: e for e in entities}
    updates = []
    for area_id in per_area:
        entity_id = f"sensor.{slug(areas[area_id]['name'])}_{slug(SUFFIX)}"
        entry = by_id.get(entity_id)
        if entry is None:
            print(f"  ! {entity_id} not found yet")
            continue
        labels = sorted(set(entry.get("labels") or []) | {EXCLUDE_LABEL})
        if entry.get("area_id") == area_id and labels == sorted(entry.get("labels") or []):
            continue
        updates.append(
            {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "area_id": area_id,
                "labels": labels,
            }
        )
    print(f"  {len(updates)} group sensors to tag with area + exclude label")
    if apply and updates:
        ws(updates)


# --------------------------------------------------------------- dashboard


def section(area_id, area_name, entity, members, excluded):
    gauge = dict(FRIDGE_GAUGE if area_id in COLD_AREAS else GAUGE)
    cards = [
        {"type": "heading", "heading": area_name, "heading_style": "title"},
        {
            "type": "gauge",
            "entity": entity,
            "name": "Average",
            "min": gauge["min"],
            "max": gauge["max"],
            "needle": True,
            "severity": {
                "green": gauge["green"],
                "yellow": gauge["yellow"],
                "red": gauge["red"],
            },
            "grid_options": {"columns": "full"},
        },
        {
            "type": "tile",
            "entity": SENSORS_TOGGLE,
            "name": "Sensors",
            "icon": "mdi:format-list-bulleted",
            "hide_state": True,
            "tap_action": {"action": "toggle"},
            "grid_options": {"columns": 6},
        },
    ]
    # The offline button only appears while this room actually has a dark
    # member — a core visibility condition, evaluated live.
    if members:
        cards.append(
            {
                "type": "tile",
                "entity": OFFLINE_TOGGLE,
                "name": "Offline",
                "icon": "mdi:lan-disconnect",
                "hide_state": True,
                "tap_action": {"action": "toggle"},
                "grid_options": {"columns": 6},
                "visibility": [
                    {
                        "condition": "or",
                        "conditions": [
                            {
                                "condition": "state",
                                "entity": m,
                                "state": ["unavailable", "unknown"],
                            }
                            for m in members
                        ],
                    }
                ],
            }
        )
    for member in members:
        cards.append(
            {
                "type": "tile",
                "entity": member,
                "state_content": ["state", "last_changed"],
                "grid_options": {"columns": "full"},
                "visibility": [
                    {
                        "condition": "or",
                        "conditions": [
                            {"condition": "state", "entity": SENSORS_TOGGLE, "state": "on"},
                            {
                                "condition": "and",
                                "conditions": [
                                    {"condition": "state", "entity": OFFLINE_TOGGLE, "state": "on"},
                                    {
                                        "condition": "state",
                                        "entity": member,
                                        "state": ["unavailable", "unknown"],
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        )
    for member in excluded:
        cards.append(
            {
                "type": "tile",
                "entity": member,
                "icon": "mdi:sigma-lower",
                "color": "disabled",
                "state_content": ["state", "last_changed"],
                "grid_options": {"columns": "full"},
                "visibility": [
                    {"condition": "state", "entity": SENSORS_TOGGLE, "state": "on"}
                ],
            }
        )
    return {"type": "grid", "cards": cards}


def dashboard(areas, per_area, excluded):
    sections = []
    for area_id, members in per_area.items():
        name = areas[area_id]["name"]
        entity = f"sensor.{slug(name)}_{slug(SUFFIX)}"
        sections.append(
            section(area_id, name, entity, members, excluded.get(area_id, []))
        )
    history = {
        "type": "grid",
        "column_span": 3,
        "cards": [
            {"type": "heading", "heading": "All rooms · 24 h", "heading_style": "title"},
            {
                "type": "history-graph",
                "hours_to_show": 24,
                "entities": [
                    {
                        "entity": f"sensor.{slug(areas[a]['name'])}_{slug(SUFFIX)}",
                        "name": areas[a]["name"],
                    }
                    for a in per_area
                ],
                "grid_options": {"columns": "full"},
            },
        ],
    }
    return {
        "views": [
            {
                "title": "Rooms",
                "path": "rooms",
                "icon": "mdi:thermometer",
                "type": "sections",
                "max_columns": 3,
                "sections": sections,
            },
            {
                "title": "History",
                "path": "history",
                "icon": "mdi:chart-line",
                "type": "sections",
                "max_columns": 1,
                "sections": [history],
            },
        ]
    }


def push_dashboard(config):
    existing = ws([{"type": "lovelace/dashboards/list"}])[0]
    if not any(d.get("url_path") == DASHBOARD_URL_PATH for d in existing):
        ws(
            [
                {
                    "type": "lovelace/dashboards/create",
                    "url_path": DASHBOARD_URL_PATH,
                    "title": DASHBOARD_TITLE,
                    "icon": DASHBOARD_ICON,
                    "show_in_sidebar": True,
                    "require_admin": False,
                }
            ]
        )
        print("dashboard created")
    ws(
        [
            {
                "type": "lovelace/config/save",
                "url_path": DASHBOARD_URL_PATH,
                "config": config,
            }
        ]
    )
    print(f"dashboard saved: /{DASHBOARD_URL_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually create/update")
    parser.add_argument("--push", action="store_true", help="also push the dashboard")
    args = parser.parse_args()

    areas, per_area, excluded = survey()
    existing = group_entries()

    print(f"{len(per_area)} areas\n")
    for area_id, members in per_area.items():
        name = areas[area_id]["name"]
        title = f"{name} {SUFFIX}"
        entry = existing.get(title)
        verb = "update" if entry else "create"
        skipped = len(excluded.get(area_id, []))
        print(
            f"  {name:18} {len(members):>2} members"
            + (f" (+{skipped} excluded)" if skipped else "")
            + f"  -> {verb}"
        )
        if not args.apply:
            continue
        entry_id = entry["entry_id"] if entry else create_group(title, members)
        set_group_options(entry_id, members)

    print("\nhelpers:")
    ensure_toggles(args.apply)
    tag_entities(areas, per_area, args.apply)
    if not args.apply:
        print("\n(dry run — nothing changed; pass --apply)")

    if args.push:
        push_dashboard(dashboard(areas, per_area, excluded))


if __name__ == "__main__":
    main()
