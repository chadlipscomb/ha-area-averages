"""Registry walking shared by the sensor platform and the config flow.

Both need the same answer to "which entities could belong to the average
for this area and this device class?" -- the sensor to compute membership,
the flow to fill the entity picker. Asking it in two places is how the two
drift apart, so it is asked here.

Everything in this module is a @callback: registry reads only, no I/O.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.util import slugify

from . import calc
from .const import DOMAIN


@callback
def resolve_label_ids(hass: HomeAssistant, configured: str) -> set[str]:
    """Label registry ids matching a configured label id, name or slug.

    Deliberately forgiving: the option used to be free text, so an entry
    saved as "exclude-average" must keep working after the picker landed.
    Shared with the options flow so both agree on what a stored value means.
    """
    wanted = (configured or "").strip()
    lowered = wanted.lower()
    if not wanted:
        return set()
    ids: set[str] = set()
    for label in lr.async_get(hass).labels.values():
        if (
            label.label_id == wanted
            or label.name.strip().lower() == lowered
            or label.label_id == slugify(lowered)
        ):
            ids.add(label.label_id)
    return ids


@callback
def candidate_index(
    hass: HomeAssistant, device_classes: set[str]
) -> dict[tuple[str, str], list[calc.Candidate]]:
    """Every sensor that could join an average, bucketed by (area, class).

    Diagnostic and config entities are INCLUDED here and sorted out by
    calc.select_members, which is the only code that gets to decide what a
    mode means. Disabled entities and our own averages never appear.

    An entity's area is its own if it has one, otherwise its device's --
    the same rule Home Assistant shows in the UI.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    buckets: dict[tuple[str, str], list[calc.Candidate]] = {}

    for reg_entry in list(ent_reg.entities.values()):
        if reg_entry.domain != "sensor" or reg_entry.platform == DOMAIN:
            continue
        if reg_entry.disabled_by is not None:
            continue
        device_class = reg_entry.device_class or reg_entry.original_device_class
        if device_class not in device_classes:
            continue

        device = (
            dev_reg.async_get(reg_entry.device_id) if reg_entry.device_id else None
        )
        area_id = reg_entry.area_id or (device.area_id if device else None)
        if not area_id:
            continue

        labels = set(reg_entry.labels)
        if device:
            labels |= set(device.labels)

        category = reg_entry.entity_category
        buckets.setdefault((area_id, device_class), []).append(
            calc.Candidate(
                reg_entry.entity_id,
                frozenset(labels),
                category.value if hasattr(category, "value") else category,
            )
        )

    for candidates in buckets.values():
        candidates.sort()
    return buckets


@callback
def battery_entities(
    hass: HomeAssistant, member_ids: list[str]
) -> dict[str, list[str]]:
    """{member entity_id: battery entities living on the same device}."""
    ent_reg = er.async_get(hass)
    found: dict[str, list[str]] = {}
    for member_id in member_ids:
        reg_entry = ent_reg.async_get(member_id)
        if reg_entry is None or not reg_entry.device_id:
            continue
        batteries = [
            sibling.entity_id
            for sibling in er.async_entries_for_device(ent_reg, reg_entry.device_id)
            if sibling.domain in ("sensor", "binary_sensor")
            and (sibling.device_class or sibling.original_device_class) == "battery"
        ]
        if batteries:
            found[member_id] = batteries
    return found


@callback
def area_sensor_ids(
    hass: HomeAssistant, area_id: str, device_class: str
) -> list[str]:
    """Entity ids the include-mode picker offers for one average.

    Diagnostic entities are offered too: include mode is an explicit
    choice, and a device's own probe is sometimes exactly the sensor you
    want. Nothing outside the area is offered -- an average is area-scoped.
    """
    bucket = candidate_index(hass, {device_class}).get((area_id, device_class), [])
    return [candidate.entity_id for candidate in bucket]
