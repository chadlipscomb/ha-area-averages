#!/usr/bin/env python3
"""Compare the YAML twin against the integration, sensor by sensor.

Two independent implementations of the same calculation are worth having
precisely because they can be diffed: if they disagree, at least one is wrong.
Run after changing either side.

    python3 yaml-edition/compare.py          # table + verdict
    python3 yaml-edition/compare.py -v       # also print matching issue strings

Exit code 1 if anything disagrees. Stdlib only.
"""

from __future__ import annotations

import json
import os
import pathlib
import ssl
import sys
import urllib.request

BASE = os.environ.get("HA_URL", "https://homeassistant.local:8123").rstrip("/")
TOKEN = os.environ.get("HA_TOKEN") or (
    pathlib.Path("~/.config/ha-token").expanduser().read_text().strip()
)
SUFFIX = "_yaml_average_temperature"

#: Compared exactly. trend/trend_rate are integration-only by design, and
#: `stale` is not comparable because the YAML clock resets on restart.
FIELDS = [
    "member_count", "reporting", "summary", "healthy",
    "minimum", "maximum", "spread", "issue",
]
LISTS = ["sensors", "excluded", "offline"]


def states():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"{BASE}/api/states", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return {s["entity_id"]: s for s in json.loads(resp.read().decode())}


def main() -> None:
    verbose = "-v" in sys.argv
    st = states()
    pairs = []
    for entity_id in sorted(st):
        if entity_id.endswith(SUFFIX):
            peer = entity_id.replace("_yaml_average_", "_average_")
            if peer in st:
                pairs.append((entity_id, peer))
            else:
                print(f"  ! no integration counterpart for {entity_id}")

    if not pairs:
        print("no comparable pairs found")
        sys.exit(1)

    print(f"{'area':18} {'YAML':>8} {'integration':>12} {'delta':>7}  fields")
    problems = []
    for yaml_id, peer_id in pairs:
        name = yaml_id[len("sensor.") : -len(SUFFIX)]
        ya, ia = st[yaml_id]["attributes"], st[peer_id]["attributes"]
        try:
            delta = abs(float(st[yaml_id]["state"]) - float(st[peer_id]["state"]))
            delta_text = f"{delta:.2f}"
            value_ok = delta < 0.01
        except ValueError:
            # Both unknown is agreement; one unknown is not.
            value_ok = st[yaml_id]["state"] == st[peer_id]["state"]
            delta_text = "-" if value_ok else "MISMATCH"

        bad = [] if value_ok else ["state"]
        for field in FIELDS:
            if ya.get(field) != ia.get(field):
                bad.append(field)
        for field in LISTS:
            mine = sorted(ya.get(field) or [])
            theirs = sorted(ia.get(field) or [])
            if field == "excluded":
                # The twin's own sensors carry the exclude label — that is what
                # keeps the integration from averaging them — so they show up in
                # its `excluded` list by design. Not a disagreement.
                theirs = [e for e in theirs if not e.endswith(SUFFIX)]
            if mine != theirs:
                bad.append(field)

        if bad:
            problems.append((name, bad, ya, ia))
        print(
            f"{name:18} {st[yaml_id]['state']:>8} {st[peer_id]['state']:>12} "
            f"{delta_text:>7}  {'all match' if not bad else ', '.join(bad)}"
        )
        if verbose and ya.get("issue"):
            print(f"{'':18} issue: {ya['issue']}")

    print()
    if problems:
        print(f"{len(problems)} of {len(pairs)} areas disagree:\n")
        for name, bad, ya, ia in problems:
            print(f"  {name}")
            for field in bad:
                if field == "state":
                    continue
                print(f"    {field}:")
                print(f"      yaml: {ya.get(field)!r}")
                print(f"      intg: {ia.get(field)!r}")
        sys.exit(1)
    print(f"all {len(pairs)} areas agree on state and every compared attribute")


if __name__ == "__main__":
    main()
