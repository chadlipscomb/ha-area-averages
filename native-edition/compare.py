#!/usr/bin/env python3
"""Compare the native Group helpers against the integration.

Only the mean is comparable — that is the whole point of the exercise. The
native helper has no notion of issues, staleness, batteries or coverage, so
this checks the one number both produce and reports what the native side
simply does not have.

    python3 native-edition/compare.py

Exit code 1 if any mean disagrees by more than 0.01. Stdlib only.
"""

from __future__ import annotations

import json
import pathlib
import ssl
import sys
import urllib.request

BASE = "https://homeassistant.local:8123"
TOKEN = pathlib.Path("~/.config/ha-token").expanduser().read_text().strip()
SUFFIX = "_native_average_temperature"


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
    st = states()
    rows, bad = [], []
    for entity_id in sorted(st):
        if not entity_id.endswith(SUFFIX):
            continue
        peer = entity_id.replace("_native_average_", "_average_")
        if peer not in st:
            print(f"  ! no integration counterpart for {entity_id}")
            continue
        rows.append((entity_id, peer))

    print(f"{'area':18} {'native':>9} {'integration':>12} {'delta':>7}  members")
    for native_id, peer_id in rows:
        name = native_id[len("sensor.") : -len(SUFFIX)]
        native, integ = st[native_id], st[peer_id]
        try:
            delta = abs(float(native["state"]) - float(integ["state"]))
            text = f"{delta:.2f}"
            ok = delta < 0.01
        except ValueError:
            ok = native["state"] == integ["state"]
            text = "-" if ok else "MISMATCH"
        # The group lists members in `entity_id`; the integration in `sensors`.
        n_members = len(native["attributes"].get("entity_id") or [])
        i_members = integ["attributes"].get("member_count")
        if not ok or n_members != i_members:
            bad.append(name)
        print(
            f"{name:18} {native['state']:>9} {integ['state']:>12} {text:>7}  "
            f"{n_members}/{i_members}"
        )

    print()
    missing = ["issue", "issues", "summary", "reporting", "offline", "stale",
               "low_battery", "healthy", "minimum", "maximum", "spread", "trend"]
    example = st[rows[0][0]]["attributes"] if rows else {}
    absent = [a for a in missing if a not in example]
    print("attributes the native helper does not have:")
    print("  " + ", ".join(absent))
    print("  (it publishes only: " + ", ".join(sorted(example)) + ")")

    if bad:
        print(f"\ndisagreements: {bad}")
        sys.exit(1)
    print(f"\nall {len(rows)} areas agree on the mean and the member count")


if __name__ == "__main__":
    main()
