#!/usr/bin/env python3
"""Build tools/fixture.json — a trimmed snapshot of live HA state, shaped like
the frontend `hass` object, so the dashboard strategy can be exercised under
node with no browser and no login.

Stdlib only.  REST (/api/states + /api/template) for state, plus a tiny
built-in websocket client for the three registries, because area icons,
floors, labels and device names exist nowhere else.  Token from
~/.config/ha-token.

    python3 tools/make_fixture.py                 # refresh the fixture
    python3 tools/make_fixture.py --no-backfill   # only what HA publishes today
    python3 tools/make_fixture.py --stale-hours 1 # match a non-default option

CONTRACT BACKFILL: the frontend is written against attributes the deployed
integration may not publish yet.  Any missing contract attribute is computed
here from the member states using custom_components/area_averages/calc.py --
the same module the sensor uses, so the fixture cannot invent semantics of
its own.  Every backfilled name is printed.  After deploying the integration,
re-run with --no-backfill: nothing should be backfilled any more.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import pathlib
import socket
import ssl
import struct
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "custom_components" / "area_averages"))
import calc  # noqa: E402  (stdlib-only, no homeassistant imports)

HOST = "homeassistant.local"
PORT = 8123
BASE = f"https://{HOST}:{PORT}"
TOKEN = pathlib.Path("~/.config/ha-token").expanduser().read_text().strip()
OUT = pathlib.Path(__file__).resolve().parent / "fixture.json"

# Both reveal controls: the legacy helper and the switch the integration owns.
TOGGLES = (
    "switch.area_averages_show_offline",
    "input_boolean.show_offline_sensors",
)

#: Attributes the frontend contract expects on every average.
CONTRACT_ATTRS = (
    "offline",
    "summary",
    "healthy",
    "minimum",
    "maximum",
    "spread",
    "trend",
    "trend_rate",
)

ROOMS_TEMPLATE = """
{%- set out = namespace(rows=[]) -%}
{%- for e in integration_entities('area_averages') -%}
  {#- The integration also owns switch.area_averages_show_offline; only the
      average SENSORS are rooms, so filter on domain + device_class. -#}
  {%- if e[:7] == 'sensor.' and state_attr(e, 'device_class') -%}
  {%- set out.rows = out.rows + [{
      'avg': e,
      'area_id': area_id(e),
      'area_name': area_name(e),
      'members': state_attr(e, 'sensors') or [],
      'excluded': state_attr(e, 'excluded') or []
  }] -%}
  {%- endif -%}
{%- endfor -%}
{{ out.rows | to_json }}
"""


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(path: str):
    req = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(req, context=_ctx(), timeout=20) as resp:
        return json.loads(resp.read().decode())


def _template(template: str):
    body = json.dumps({"template": template}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/template",
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, context=_ctx(), timeout=20) as resp:
        return json.loads(resp.read().decode())


# ------------------------------------------------------- websocket (stdlib)


def _ws_send(sock: ssl.SSLSocket, payload: bytes) -> None:
    """One masked text frame (RFC 6455 requires client frames to be masked)."""
    mask = os.urandom(4)
    size = len(payload)
    header = b"\x81"
    if size < 126:
        header += bytes([0x80 | size])
    elif size < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", size)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", size)
    sock.sendall(
        header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    )


def _ws_fill(sock: ssl.SSLSocket, want: int, buf: bytes) -> bytes:
    while len(buf) < want:
        chunk = sock.recv(1 << 16)
        if not chunk:
            raise EOFError("websocket closed mid-frame")
        buf += chunk
    return buf


def _ws_recv(sock: ssl.SSLSocket, state: dict) -> str:
    """Next text payload; skips ping/pong, raises on close."""
    buf = state["buf"]
    while True:
        buf = _ws_fill(sock, 2, buf)
        opcode = buf[0] & 0x0F
        length = buf[1] & 0x7F
        offset = 2
        if length == 126:
            buf = _ws_fill(sock, 4, buf)
            length = struct.unpack("!H", buf[2:4])[0]
            offset = 4
        elif length == 127:
            buf = _ws_fill(sock, 10, buf)
            length = struct.unpack("!Q", buf[2:10])[0]
            offset = 10
        buf = _ws_fill(sock, offset + length, buf)
        payload = buf[offset : offset + length]
        buf = buf[offset + length :]
        state["buf"] = buf
        if opcode in (1, 2):
            return payload.decode()
        if opcode == 8:
            raise EOFError("websocket closed by server")


def ws(commands: list[dict]) -> list[dict]:
    """Run a list of websocket commands in order; return their results."""
    raw = socket.create_connection((HOST, PORT), timeout=30)
    sock = _ctx().wrap_socket(raw, server_hostname=HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET /api/websocket HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("no HTTP response from /api/websocket")
        head += chunk
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise RuntimeError(f"websocket upgrade refused: {head.splitlines()[0]!r}")

    state = {"buf": head.split(b"\r\n\r\n", 1)[1]}
    _ws_recv(sock, state)  # auth_required
    _ws_send(sock, json.dumps({"type": "auth", "access_token": TOKEN}).encode())
    reply = json.loads(_ws_recv(sock, state))
    if reply.get("type") != "auth_ok":
        raise RuntimeError(f"websocket auth failed: {reply}")

    results: list[dict] = []
    try:
        for index, command in enumerate(commands, start=1):
            _ws_send(sock, json.dumps(dict(command, id=index)).encode())
            while True:
                message = json.loads(_ws_recv(sock, state))
                if message.get("id") == index and message.get("type") == "result":
                    if not message.get("success", False):
                        raise RuntimeError(
                            f"{command['type']} failed: {message.get('error')}"
                        )
                    results.append(message["result"])
                    break
    finally:
        sock.close()
    return results


# ---------------------------------------------------------------- backfill


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _number(text) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def backfill(rooms, states, all_states, stale_hours: float) -> set[str]:
    """Fill in contract attributes the deployed integration does not publish.

    Returns the set of attribute names that had to be computed here.  The
    state VALUE is never rewritten -- that stays live truth.
    """
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    stale_seconds = stale_hours * 3600.0
    filled: set[str] = set()

    for room in rooms:
        entry = states.get(room["avg"])
        if entry is None:
            continue
        attrs = entry["attributes"]
        device_class = attrs.get("device_class") or ""
        target_unit = attrs.get("unit_of_measurement")
        members = list(attrs.get("sensors") or [])

        samples = []
        names: dict[str, str] = {}
        for member in members:
            live = all_states.get(member)
            member_attrs = (live or {}).get("attributes", {})
            names[member] = member_attrs.get("friendly_name") or member
            value = None
            last_numeric = None
            if live is not None:
                raw = _number(live.get("state"))
                if raw is not None:
                    value = calc.convert_value(
                        raw,
                        member_attrs.get("unit_of_measurement"),
                        target_unit,
                        device_class,
                    )
                    last_numeric = _epoch(
                        live.get("last_reported") or live.get("last_updated")
                    )
            samples.append(calc.MemberSample(member, value, last_numeric))

        reporting, stale, offline = calc.classify_members(
            samples, now, stale_seconds
        )
        values = [value for _, value in reporting]
        minimum, maximum, spread = calc.stats(values)

        # The deployed build publishes `reporting`/`stale` with the OLD
        # semantics (stale members still counted in the mean).  Recompute
        # them alongside `offline` so the invariant
        # len(offline) + len(stale) + reporting == member_count holds.
        legacy = "offline" not in attrs
        stale_rows = []
        for entity_id in stale:
            last = _epoch(
                (all_states.get(entity_id) or {}).get("last_reported")
            )
            hours = int((now - last) // 3600) if last else int(stale_hours)
            stale_rows.append((names.get(entity_id, entity_id), entity_id, hours))
        battery_rows = [
            row for row in (attrs.get("issues") or []) if "battery" in row
        ]
        issues = calc.issue_rows(
            [(names.get(e, e), e) for e in offline], stale_rows, []
        ) + battery_rows

        computed = {
            "offline": offline,
            "summary": calc.summary_text(len(members), len(reporting)),
            "healthy": calc.healthy_state(len(reporting), issues),
            "minimum": minimum,
            "maximum": maximum,
            "spread": spread,
            # A one-shot snapshot has no history: "unknown" is the honest
            # answer and matches what the sensor reports after a restart.
            "trend": calc.TREND_UNKNOWN,
            "trend_rate": None,
        }
        for name in CONTRACT_ATTRS:
            if name not in attrs:
                attrs[name] = computed[name]
                filled.add(name)

        if legacy:
            attrs["reporting"] = len(reporting)
            attrs["stale"] = stale
            attrs["issues"] = issues
            attrs["issue"] = "; ".join(issues) if issues else None
            filled.update({"reporting", "stale", "issues", "issue"})

    return filled


# -------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help="write only the attributes HA actually publishes",
    )
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=24.0,
        help="staleness window used by the backfill (match the HA option)",
    )
    args = parser.parse_args()

    rooms = _template(ROOMS_TEMPLATE)
    all_states = {s["entity_id"]: s for s in _get("/api/states")}

    try:
        area_rows, device_rows, entity_rows = ws(
            [
                {"type": "config/area_registry/list"},
                {"type": "config/device_registry/list"},
                {"type": "config/entity_registry/list"},
            ]
        )
    except Exception as err:  # noqa: BLE001 - fail loudly, never write nulls
        raise SystemExit(
            f"registry websocket failed ({err.__class__.__name__}: {err}).\n"
            "Refusing to write a fixture with null area icons and empty "
            "devices — that has already produced a confident, wrong review."
        ) from err

    areas_live = {a["area_id"]: a for a in area_rows}
    devices_live = {d["id"]: d for d in device_rows}
    entities_live = {e["entity_id"]: e for e in entity_rows}

    keep: set[str] = set(TOGGLES)
    for room in rooms:
        keep.add(room["avg"])
        keep.update(room["members"])
        keep.update(room["excluded"])

    states: dict[str, dict] = {}
    for entity_id in sorted(keep):
        state = all_states.get(entity_id)
        if state is None:
            continue
        states[entity_id] = {
            "entity_id": entity_id,
            "state": state["state"],
            "attributes": state["attributes"],
            "last_changed": state.get("last_changed"),
            "last_reported": state.get("last_reported")
            or state.get("last_updated"),
        }

    entities: dict[str, dict] = {}
    areas: dict[str, dict] = {}
    devices: dict[str, dict] = {}

    def _add_entity(entity_id: str, fallback_area: str | None) -> None:
        live = entities_live.get(entity_id) or {}
        device_id = live.get("device_id")
        entities[entity_id] = {
            "entity_id": entity_id,
            "platform": live.get("platform") or "other",
            "area_id": live.get("area_id") or fallback_area,
            "device_id": device_id,
            "labels": list(live.get("labels") or []),
        }
        if device_id and device_id in devices_live and device_id not in devices:
            device = devices_live[device_id]
            devices[device_id] = {
                "id": device_id,
                "name": device.get("name"),
                "name_by_user": device.get("name_by_user"),
                "area_id": device.get("area_id"),
            }

    for room in rooms:
        area_id = room["area_id"]
        _add_entity(room["avg"], area_id)
        for member in list(room["members"]) + list(room["excluded"]):
            if member not in entities:
                _add_entity(member, area_id)
        if area_id:
            live_area = areas_live.get(area_id) or {}
            areas[area_id] = {
                "area_id": area_id,
                "name": live_area.get("name") or room["area_name"],
                "icon": live_area.get("icon"),
                "floor_id": live_area.get("floor_id"),
                "labels": list(live_area.get("labels") or []),
            }

    filled: set[str] = set()
    if not args.no_backfill:
        filled = backfill(rooms, states, all_states, args.stale_hours)

    fixture = {
        "states": states,
        "entities": entities,
        "areas": areas,
        "devices": devices,
    }
    OUT.write_text(json.dumps(fixture, indent=1, sort_keys=True))

    offline = sum(
        1
        for s in states.values()
        if s["state"] in ("unavailable", "unknown")
        and s["entity_id"].startswith("sensor.")
    )
    missing_icons = [a for a, v in areas.items() if not v.get("icon")]
    print(
        f"{OUT.name}: {len(rooms)} averages, {len(states)} states "
        f"({offline} offline members), {len(areas)} areas, "
        f"{len(devices)} devices"
    )
    if missing_icons:
        print(f"  note: {len(missing_icons)} areas have no icon: {missing_icons}")
    if filled:
        print(
            "  WARNING: backfilled from calc.py (the deployed integration does "
            f"not publish these yet): {', '.join(sorted(filled))}"
        )
        print(
            f"  staleness window assumed {args.stale_hours} h — pass "
            "--stale-hours to match the live option; re-run with "
            "--no-backfill after deploying to verify."
        )


if __name__ == "__main__":
    main()
