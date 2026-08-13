#!/usr/bin/env python3
"""Set up / update the "Area Temperatures" dashboard.

Default (--push) wires the dashboard to the **custom strategy**
(www/area_averages_strategy.js): registers the /local/ resource with a
content-hash version (bump-on-change), makes sure the dashboard and the
exclude label exist, and saves the two-line strategy config. The strategy
then regenerates every section live on each dashboard load — new sensors,
new areas, exclusions, offline-hiding. Ship the JS first via ../deploy.sh.

`--push-static` pushes the old generated snapshot instead (emergency
fallback if the strategy JS misbehaves; no offline-hiding, goes stale).

Usage:
  python3 gen_dashboard.py            # print the static lovelace JSON
  python3 gen_dashboard.py --push     # strategy mode (the normal path)
  python3 gen_dashboard.py --push-static

Needs `websockets` for pushing. Token comes from ~/.config/ha-token.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import ssl
import sys
import urllib.request

BASE = "https://homeassistant.local:8123"
WS_URL = "wss://homeassistant.local:8123/api/websocket"
TOKEN = pathlib.Path("~/.config/ha-token").expanduser().read_text().strip()

DASHBOARD_URL_PATH = "room-temperatures"
DASHBOARD_TITLE = "Area Temperatures"
DASHBOARD_ICON = "mdi:thermometer-lines"
EXCLUDE_LABEL = "exclude-average"

STATUS_MARKDOWN = """\
{% set ns = namespace(rows=[]) %}
{% for e in integration_entities('area_averages') | sort %}
{% if e.startswith('sensor.') %}
{% for i in state_attr(e, 'issues') or [] %}
{% set ns.rows = ns.rows + [(area_name(e) or e) ~ ' — ' ~ i] %}
{% endfor %}
{% endif %}
{% endfor %}
{% if ns.rows %}
#### ⚠️ {{ ns.rows | count }} sensor issue{{ 's' if ns.rows | count > 1 else '' }}
{% for r in ns.rows %}
- {{ r }}
{% endfor %}
{% else %}
#### ✅ All sensors healthy
Every sensor has reported recently and no batteries are low.
{% endif %}
"""


def _rest_template(template: str):
    body = json.dumps({"template": template}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/template",
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        return json.loads(resp.read().decode())


def fetch_rooms():
    """area name -> {avg, members} from the live integration entities."""
    rows = _rest_template(
        """
{%- set out = namespace(rows=[]) -%}
{%- for e in integration_entities('area_averages') | sort -%}
  {%- if state_attr(e, 'device_class') == 'temperature' -%}
    {%- set out.rows = out.rows + [{
        'avg': e,
        'area': area_name(e) or e,
        'members': state_attr(e, 'sensors') or [],
        'excluded': state_attr(e, 'excluded') or []
    }] -%}
  {%- endif -%}
{%- endfor -%}
{{ out.rows | to_json }}
"""
    )
    return sorted(rows, key=lambda r: r["area"].lower())


def build_config(rooms):
    room_sections = []
    history_sections = []
    for room in rooms:
        cards = [
            {"type": "heading", "heading": room["area"], "heading_style": "title"},
            {
                "type": "tile",
                "entity": room["avg"],
                "name": "Room average",
                "icon": "mdi:thermometer-auto",
                "color": "deep-orange",
                # `issue` is now a joined list of every offline/stale/battery
                # row, far too long for a tile; `summary` is the one-phrase
                # coverage string ("2 of 9 sensors").
                "state_content": ["state", "summary"],
                "grid_options": {"columns": "full"},
            },
        ]
        for member in room["members"]:
            cards.append(
                {
                    "type": "tile",
                    "entity": member,
                    "state_content": ["state", "last_changed"],
                }
            )
        # Excluded sensors stay visible, just greyed out.
        for excluded in room.get("excluded", []):
            cards.append(
                {
                    "type": "tile",
                    "entity": excluded,
                    "icon": "mdi:sigma-lower",
                    "color": "disabled",
                    "state_content": ["state", "last_changed"],
                }
            )
        room_sections.append({"type": "grid", "cards": cards})

        history_sections.append(
            {
                "type": "grid",
                "cards": [
                    {
                        "type": "heading",
                        "heading": room["area"],
                        "heading_style": "title",
                    },
                    {
                        "type": "history-graph",
                        "hours_to_show": 24,
                        "entities": [{"entity": room["avg"]}]
                        + [{"entity": m} for m in room["members"]],
                        "grid_options": {"columns": "full"},
                    },
                ],
            }
        )

    status_section = {
        "type": "grid",
        "cards": [
            {"type": "heading", "heading": "Status", "heading_style": "title"},
            {
                "type": "markdown",
                "content": STATUS_MARKDOWN,
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
                "sections": [status_section] + room_sections,
            },
            {
                "title": "History",
                "path": "history",
                "icon": "mdi:chart-line",
                "type": "sections",
                "max_columns": 2,
                "sections": history_sections,
            },
        ]
    }


STRATEGY_JS = pathlib.Path(__file__).resolve().parent.parent / "www" / "area_averages_strategy.js"


def strategy_config():
    return {"strategy": {"type": "custom:area-temperatures", "history_hours": 24}}


def resource_url():
    import hashlib

    digest = hashlib.sha1(STRATEGY_JS.read_bytes()).hexdigest()[:8]
    return f"/local/area_averages_strategy.js?v={digest}"


async def push(config, ensure_resource=False):
    import websockets

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(WS_URL, ssl=ctx, max_size=2**24) as ws:
        counter = 0

        async def call(message):
            nonlocal counter
            counter += 1
            message["id"] = counter
            await ws.send(json.dumps(message))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == counter and reply.get("type") == "result":
                    return reply

        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise SystemExit("websocket auth failed")

        if ensure_resource:
            # Register/refresh the strategy module resource (hash-versioned
            # URL busts the browser cache on every JS change).
            url = resource_url()
            resources = await call({"type": "lovelace/resources"})
            mine = [
                r
                for r in resources.get("result", [])
                if "area_averages_strategy.js" in r.get("url", "")
            ]
            if not mine:
                created = await call(
                    {"type": "lovelace/resources/create", "res_type": "module", "url": url}
                )
                print("resource created:", created["success"], url)
            elif mine[0]["url"] != url:
                updated = await call(
                    {
                        "type": "lovelace/resources/update",
                        "resource_id": mine[0]["id"],
                        "res_type": "module",
                        "url": url,
                    }
                )
                print("resource updated:", updated["success"], url)
            else:
                print("resource up to date:", url)

        # Exclude label: create only if missing.
        labels = await call({"type": "config/label_registry/list"})
        if not any(
            label["name"].lower() == EXCLUDE_LABEL
            for label in labels.get("result", [])
        ):
            created = await call(
                {
                    "type": "config/label_registry/create",
                    "name": EXCLUDE_LABEL,
                    "icon": "mdi:sigma-lower",
                    "color": "red",
                }
            )
            print("label created:", created["success"])

        # Dashboard: create only if missing, then save the config.
        dashboards = await call({"type": "lovelace/dashboards/list"})
        existing = [
            d
            for d in dashboards.get("result", [])
            if d.get("url_path") == DASHBOARD_URL_PATH
        ]
        if not existing:
            created = await call(
                {
                    "type": "lovelace/dashboards/create",
                    "url_path": DASHBOARD_URL_PATH,
                    "title": DASHBOARD_TITLE,
                    "icon": DASHBOARD_ICON,
                    "show_in_sidebar": True,
                    "require_admin": False,
                }
            )
            print("dashboard created:", created["success"])
        saved = await call(
            {
                "type": "lovelace/config/save",
                "url_path": DASHBOARD_URL_PATH,
                "config": config,
            }
        )
        print("config saved:", saved["success"])


def main():
    if "--push" in sys.argv:
        asyncio.run(push(strategy_config(), ensure_resource=True))
        print(f"{BASE}/{DASHBOARD_URL_PATH}  (strategy mode — sections regenerate on load)")
    elif "--push-static" in sys.argv:
        rooms = fetch_rooms()
        asyncio.run(push(build_config(rooms)))
        print(f"{BASE}/{DASHBOARD_URL_PATH}  ({len(rooms)} rooms, static snapshot)")
    else:
        print(json.dumps(build_config(fetch_rooms()), indent=2))


if __name__ == "__main__":
    main()
