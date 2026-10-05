"""Pure-stdlib calculations shared by the sensor platform and the tooling.

This module is the single definition of what the published attributes MEAN.
`sensor.py` imports it, and so does `tools/make_fixture.py` (which runs under
the plain system python, outside Home Assistant) so the test fixture and the
live sensor cannot drift apart.

Rules for this file:
  * stdlib only -- NO ``homeassistant`` imports, so it stays importable
    standalone via ``sys.path.insert(0, custom_components/area_averages)``
  * pure functions over plain data (floats, strings, tuples); no I/O, no
    logging, no clock reads -- ``now`` is always passed in
  * every function must tolerate junk: None, NaN, empty lists, strings
"""

from __future__ import annotations

import math
import unicodedata
from typing import Iterable, NamedTuple, Sequence

# --------------------------------------------------------------------- trend

TREND_UNKNOWN = "unknown"
TREND_STEADY = "steady"
TREND_RISING = "rising"
TREND_FALLING = "falling"

#: |rate| below this (native unit per hour) reads as "steady".
TREND_DEADBAND = 0.3
#: Samples must span at least this long before a trend is claimed.
TREND_MIN_SPAN = 900.0  # seconds

# ---------------------------------------------------------------------- name

#: Friendly names that say nothing about *which* sensor is broken; an issue
#: row uses the entity_id instead of one of these.
GENERIC_NAMES = frozenset(
    {
        "temperature",
        "temp",
        "humidity",
        "pressure",
        "illuminance",
        "luminance",
        "battery",
        "co2",
        "carbon dioxide",
        "pm2.5",
        "pm25",
        "sensor",
        "state",
        "value",
    }
)

# --------------------------------------------------------------------- units

#: Device classes whose average unit does not depend on the user's unit
#: system.  Temperature and pressure come from ``hass.config.units``.
STATIC_UNITS = {
    "humidity": "%",
    "carbon_dioxide": "ppm",
    "illuminance": "lx",
    # U+00B5 MICRO SIGN, matching homeassistant.const.
    "pm25": "µg/m³",
}

_TEMPERATURE_UNITS = {"°c": "C", "c": "C", "°f": "F", "f": "F", "k": "K"}

#: Pressure unit -> pascals.  Mirrors homeassistant.util.unit_conversion.
_PRESSURE_TO_PA = {
    "pa": 1.0,
    "hpa": 100.0,
    "kpa": 1000.0,
    "bar": 100000.0,
    "cbar": 1000.0,
    "mbar": 100.0,
    "mmhg": 133.322,
    "inhg": 3386.389,
    "psi": 6894.757,
}


def normalize_unit(unit: str | None) -> str | None:
    """Fold a unit string so equal units compare equal.

    NFKC maps U+00B5 MICRO SIGN onto U+03BC GREEK SMALL LETTER MU and the
    superscript digits onto plain ones, so the two spellings of
    ``µg/m³`` that Home Assistant uses stop being different units.
    Used for COMPARISON ONLY -- the published unit keeps its original
    spelling.
    """
    if unit is None:
        return None
    text = unicodedata.normalize("NFKC", str(unit)).strip()
    return text or None


def fixed_unit(
    device_class: str,
    temperature_unit: str | None = None,
    pressure_unit: str | None = None,
) -> str | None:
    """The unit an average of ``device_class`` always publishes.

    Never None for a supported device class, so a MEASUREMENT sensor can
    never be created unitless and later flip units (which makes the
    recorder throw away its long-term statistics).
    """
    if device_class == "temperature":
        return temperature_unit or "°C"
    if device_class == "pressure":
        return pressure_unit or "hPa"
    return STATIC_UNITS.get(device_class)


def convert_value(
    value: float,
    from_unit: str | None,
    to_unit: str | None,
    device_class: str,
) -> float | None:
    """Convert ``value`` into ``to_unit``; None when that is impossible.

    Stdlib mirror of Home Assistant's unit converters, used by
    tools/make_fixture.py.  ``sensor.py`` prefers the real HA converters
    (they cover more units) and only shares the normalisation rule.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None

    source = normalize_unit(from_unit)
    target = normalize_unit(to_unit)
    if target is None or source is None or source == target:
        return number

    if device_class == "temperature":
        src = _TEMPERATURE_UNITS.get(source.lower())
        dst = _TEMPERATURE_UNITS.get(target.lower())
        if src is None or dst is None:
            return None
        celsius = {
            "C": number,
            "F": (number - 32.0) / 1.8,
            "K": number - 273.15,
        }[src]
        return {
            "C": celsius,
            "F": celsius * 1.8 + 32.0,
            "K": celsius + 273.15,
        }[dst]

    if device_class == "pressure":
        src_factor = _PRESSURE_TO_PA.get(source.lower())
        dst_factor = _PRESSURE_TO_PA.get(target.lower())
        if not src_factor or not dst_factor:
            return None
        return number * src_factor / dst_factor

    return None


# ---------------------------------------------------------------- membership


class MemberSample(NamedTuple):
    """One member of an average, as seen at a single instant.

    ``value`` is already converted into the average's unit, or None when the
    member has no usable number right now.  ``last_numeric`` is the epoch
    time (seconds, UTC) of that member's most recent numeric report, or None
    when it has never produced one.
    """

    entity_id: str
    value: float | None
    last_numeric: float | None


def classify_members(
    members: Iterable[Sequence],
    now: float,
    stale_seconds: float,
) -> tuple[list[tuple[str, float]], list[str], list[str]]:
    """Split members into (reporting, stale, offline) -- always disjoint.

    OFFLINE: no usable number at all (missing state, unavailable/unknown,
    non-numeric, or a unit that will not convert).  Offline is immediate --
    there is nothing to wait for -- and it wins over staleness, so a member
    that has never reported lands here rather than in ``stale``.

    STALE: holds a convertible number, but its last actual report is older
    than the window.  Its value is DROPPED from the mean.

    REPORTING: everything else; returns (entity_id, value) pairs so callers
    can keep the ids alongside the numbers.

    Guarantees ``len(offline) + len(stale) + len(reporting) == len(members)``
    for a de-duplicated member list.
    """
    reporting: list[tuple[str, float]] = []
    stale: list[str] = []
    offline: list[str] = []

    for member in members:
        entity_id = member[0]
        value = member[1]
        last_numeric = member[2]

        number: float | None = None
        if value is not None:
            try:
                candidate = float(value)
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None and math.isfinite(candidate):
                number = candidate

        if number is None:
            offline.append(entity_id)
            continue

        if (
            stale_seconds > 0
            and last_numeric is not None
            and (now - last_numeric) >= stale_seconds
        ):
            stale.append(entity_id)
            continue

        reporting.append((entity_id, number))

    reporting.sort()
    return reporting, sorted(stale), sorted(offline)


def summary_text(member_count: int, reporting: int) -> str:
    """One phrase saying how many sensors the number is standing on."""
    try:
        total = int(member_count)
        live = int(reporting)
    except (TypeError, ValueError):
        return "no sensors reporting"
    if live <= 0:
        return "no sensors reporting"
    if total == 1 and live == 1:
        return "1 sensor"
    if live >= total:
        return f"{total} sensors"
    return f"{live} of {total} sensors"


def stats(values: Iterable[float]) -> tuple[float | None, float | None, float | None]:
    """(minimum, maximum, spread) of the values inside the mean."""
    numbers: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numbers.append(number)
    if not numbers:
        return (None, None, None)
    low = min(numbers)
    high = max(numbers)
    spread = round(high - low, 2) + 0.0 if len(numbers) >= 2 else None
    return (round(low, 2) + 0.0, round(high, 2) + 0.0, spread)


def trend_of(
    samples: Sequence[Sequence[float]],
    deadband: float = TREND_DEADBAND,
    min_span: float = TREND_MIN_SPAN,
) -> tuple[str, float | None]:
    """Least-squares direction of a (epoch_seconds, value) series.

    Returns ("unknown", None) until the samples span ``min_span``; that is
    the honest answer for the first quarter hour after every restart and
    must not be faked.  The rate is in native units per hour.
    """
    points: list[tuple[float, float]] = []
    for sample in samples or ():
        try:
            timestamp = float(sample[0])
            value = float(sample[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(timestamp) and math.isfinite(value):
            points.append((timestamp, value))

    if len(points) < 2:
        return (TREND_UNKNOWN, None)
    points.sort()
    if points[-1][0] - points[0][0] < min_span:
        return (TREND_UNKNOWN, None)

    origin = points[0][0]
    xs = [(timestamp - origin) / 3600.0 for timestamp, _ in points]
    ys = [value for _, value in points]
    count = len(points)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return (TREND_UNKNOWN, None)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    if not math.isfinite(slope):
        return (TREND_UNKNOWN, None)

    rate = round(slope, 2) + 0.0  # + 0.0 folds -0.0 into 0.0 for JSON
    if abs(rate) < deadband:
        return (TREND_STEADY, rate)
    return (TREND_RISING if rate > 0 else TREND_FALLING, rate)


# -------------------------------------------------------------------- issues


def display_name(name: str | None, entity_id: str) -> str:
    """Name for an issue row; the entity_id when the name says nothing."""
    text = (name or "").strip()
    if len(text) < 4 or text.lower() in GENERIC_NAMES:
        return entity_id
    return text


def issue_rows(
    offline: Iterable[Sequence],
    stale: Iterable[Sequence],
    battery: Iterable[Sequence],
) -> list[str]:
    """Human-readable issue rows: offline first, then stale, then battery.

    ``offline``  -- (name, entity_id)
    ``stale``    -- (name, entity_id, hours)
    ``battery``  -- (name, entity_id, level_percent or None for a binary
                    low-battery sensor)
    """
    rows: list[str] = []
    for name, entity_id in offline:
        rows.append(f"{display_name(name, entity_id)}: offline")
    for name, entity_id, hours in stale:
        try:
            whole_hours = int(hours)
        except (TypeError, ValueError):
            whole_hours = 0
        rows.append(f"{display_name(name, entity_id)}: no data for {whole_hours} h")
    for name, entity_id, level in battery:
        label = display_name(name, entity_id)
        if level is None:
            rows.append(f"{label}: low battery")
        else:
            try:
                rows.append(f"{label}: battery {int(round(float(level)))}%")
            except (TypeError, ValueError):
                rows.append(f"{label}: low battery")
    return rows


def healthy_state(reporting: int, issues: Sequence[str]) -> bool:
    """An average with nothing behind it is never healthy."""
    try:
        live = int(reporting)
    except (TypeError, ValueError):
        return False
    return live > 0 and not issues


# ---------------------------------------------------------------------- name

#: How each device class is spelled in a sensor name and a subentry title.
NICE_NAMES = {
    "temperature": "Temperature",
    "humidity": "Humidity",
    "pressure": "Pressure",
    "carbon_dioxide": "CO2",
    "illuminance": "Illuminance",
    "pm25": "PM2.5",
}


def nice_name(device_class: str) -> str:
    """"carbon_dioxide" -> "CO2"; anything unknown -> Title Case."""
    text = str(device_class or "")
    return NICE_NAMES.get(text, text.replace("_", " ").title())


def average_title(area_name: str, device_class: str) -> str:
    """The name of an average -- "Master Bathroom Average Humidity".

    One definition, used for both the entity name and the subentry title so
    the integration page and the entity list cannot disagree.
    """
    return f"{area_name} Average {nice_name(device_class)}"


# ---------------------------------------------------------------- selection

#: Every candidate in the area is a member unless it carries the exclude
#: label. This is the original behaviour and stays the default.
MODE_EXCLUDE = "exclude"
#: Only the entities the user picked (or labelled) are members.
MODE_INCLUDE = "include"

MODES = (MODE_EXCLUDE, MODE_INCLUDE)


class Candidate(NamedTuple):
    """One entity that could belong to an average, as the registry sees it.

    ``labels`` is the union of the entity's own labels and its device's --
    labelling a device excludes (or includes) everything it reports.
    ``entity_category`` is None for a normal sensor and "diagnostic" or
    "config" for the housekeeping entities a device also publishes (radio
    chip temperatures, Zigbee LQI, ...).
    """

    entity_id: str
    labels: frozenset
    entity_category: str | None


def select_members(
    candidates: Iterable[Sequence],
    mode: str,
    include_entities: Iterable[str] = (),
    include_label_ids: Iterable[str] = (),
    exclude_label_ids: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Split the candidates of one average into (members, excluded).

    EXCLUDE mode -- every candidate is a member unless it carries the
    exclude label, in which case it lands in ``excluded``.

    INCLUDE mode -- a candidate is a member only if it was picked by
    entity_id or carries the include label; every other candidate lands in
    ``excluded``.

    Diagnostic and config entities are dropped from BOTH lists: they were
    never offered to the user, so calling them "excluded" would put radio
    chip temperatures on the dashboard's "Not in average" list. The one
    exception is an entity the user picked or labelled by hand in include
    mode -- if you went and chose it, you meant it.

    Both lists are sorted and disjoint. An unknown mode is treated as
    exclude: a typo in stored data must never silently empty an average.
    """
    picked = {str(entity_id) for entity_id in include_entities or ()}
    include_labels = frozenset(include_label_ids or ())
    exclude_labels = frozenset(exclude_label_ids or ())
    include_mode = mode == MODE_INCLUDE

    members: set[str] = set()
    excluded: set[str] = set()

    for candidate in candidates or ():
        entity_id = str(candidate[0])
        labels = frozenset(candidate[1] or ())
        category = candidate[2]

        if include_mode:
            if entity_id in picked or (labels & include_labels):
                members.add(entity_id)
            elif category is None:
                excluded.add(entity_id)
            continue

        if category is not None:
            continue
        if labels & exclude_labels:
            excluded.add(entity_id)
        else:
            members.add(entity_id)

    return sorted(members), sorted(excluded - members)


# ------------------------------------------------------------------ registry


def average_unique_id(area_id: str, device_class: str) -> str:
    """The one place the unique_id format is written down."""
    return f"{area_id}_{device_class}_average"


def orphan_unique_ids(
    desired: Iterable[str],
    existing: Iterable[Sequence[str]],
) -> list[str]:
    """entity_ids in ``existing`` whose unique_id is no longer wanted.

    ``existing`` is an iterable of (entity_id, unique_id).  Pure set
    arithmetic on purpose: never parse a unique_id, because both
    ``master_bathroom`` and ``carbon_dioxide`` contain underscores.
    """
    wanted = set(desired)
    return [
        entity_id for entity_id, unique_id in existing if unique_id not in wanted
    ]


def subentry_key(area_id: str, device_class: str) -> str:
    """Stable key for one (area, device class) average.

    "|" is not legal in a slug, so it cannot collide with either half --
    unlike "_", which appears inside both ``master_bathroom`` and
    ``carbon_dioxide``. Used as the subentry unique_id and as the entry in
    the auto-discovery bookkeeping lists.
    """
    return f"{area_id}|{device_class}"


def parse_subentry_key(key: str) -> tuple[str, str] | None:
    """(area_id, device_class) from a key, or None when it is junk."""
    parts = str(key or "").split("|")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return (parts[0], parts[1])


def match_average_unique_id(
    unique_id: str,
    area_ids: Iterable[str],
    device_classes: Iterable[str],
) -> tuple[str, str] | None:
    """Recover (area_id, device_class) from an average's unique_id.

    Used by the migration to attach registry entities created before
    subentries existed. Never splits on "_" -- it rebuilds the candidate
    ids and compares, and prefers the LONGEST matching area so that
    ``master_bathroom`` wins over ``master``.
    """
    text = str(unique_id or "")
    best: tuple[str, str] | None = None
    for area_id in area_ids or ():
        for device_class in device_classes or ():
            if average_unique_id(area_id, device_class) != text:
                continue
            if best is None or len(area_id) > len(best[0]):
                best = (area_id, device_class)
    return best
