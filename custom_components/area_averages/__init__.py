"""The Area Averages integration.

One average sensor per configured average -- a config SUBENTRY holding an
area, a device class and how its members are chosen -- plus the two reveal
switches the dashboard hangs its buttons on. Pure core APIs, no external
dependencies.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er

from . import calc
from .const import (
    CONF_AREA_ID,
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_CLASSES,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_CLASSES,
    CONF_IGNORED,
    CONF_INCLUDE_ENTITIES,
    CONF_INCLUDE_LABEL,
    CONF_MODE,
    CONF_SOURCE,
    CONFIG_VERSION,
    DATA_AUTO_CREATED,
    DEFAULT_DEVICE_CLASSES,
    DOMAIN,
    SOURCE_AUTO,
    SUBENTRY_TYPE,
    SUPPORTED_DEVICE_CLASSES,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Area Averages from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to a change of options or of any subentry.

    A full reload is only worth it when a setting the manager caches has
    changed. Adding, editing or deleting a single average is synced in
    place instead, so the other averages keep their trend history.
    """
    manager = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if manager is None or manager.options_need_reload(entry):
        await hass.config_entries.async_reload(entry.entry_id)
        return
    manager.handle_entry_update()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry."""
    if entry.version > CONFIG_VERSION:
        # Downgrading: the subentries this version wrote mean nothing to an
        # older one, and guessing would delete averages.
        return False

    if entry.version == 1:
        _migrate_v1_to_v2(hass, entry)

    _LOGGER.info("Area Averages: migrated config entry to version %s", entry.version)
    return True


def _migrate_v1_to_v2(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Turn a global device-class list into one subentry per average.

    Version 1 created an average for every area that had members of the
    configured classes. Every one of those already exists in the entity
    registry, so the migration adopts them: each becomes a subentry, and
    the existing entity is attached to it. Entity ids, unique_ids and
    recorder history are untouched.

    Auto-discovery is turned ON, because discovering averages is exactly
    what version 1 did.
    """
    options = dict(entry.options)
    classes = [
        device_class
        for device_class in options.pop(CONF_DEVICE_CLASSES, DEFAULT_DEVICE_CLASSES)
        if device_class in SUPPORTED_DEVICE_CLASSES
    ]
    options[CONF_AUTO_DISCOVER] = True
    options[CONF_AUTO_DISCOVER_CLASSES] = classes or list(DEFAULT_DEVICE_CLASSES)
    options.setdefault(CONF_IGNORED, [])

    ent_reg = er.async_get(hass)
    area_reg = ar.async_get(hass)
    area_ids = list(area_reg.areas)

    adopted: list[str] = []
    assignments: list[tuple[str, str]] = []
    seen: set[str] = set()

    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.domain != "sensor" or reg_entry.platform != DOMAIN:
            continue
        if not str(reg_entry.unique_id).endswith("_average"):
            continue
        match = calc.match_average_unique_id(
            reg_entry.unique_id, area_ids, SUPPORTED_DEVICE_CLASSES
        )
        if match is None:
            _LOGGER.warning(
                "Area Averages: could not work out which area %s belongs to "
                "(unique_id %r). It is left alone; delete it if it is stale.",
                reg_entry.entity_id,
                reg_entry.unique_id,
            )
            continue
        area_id, device_class = match
        key = calc.subentry_key(area_id, device_class)
        if key in seen:
            continue
        seen.add(key)

        area = area_reg.async_get_area(area_id)
        subentry = ConfigSubentry(
            data={
                CONF_AREA_ID: area_id,
                CONF_DEVICE_CLASS: device_class,
                CONF_MODE: calc.MODE_EXCLUDE,
                CONF_INCLUDE_ENTITIES: [],
                CONF_INCLUDE_LABEL: None,
                CONF_SOURCE: SOURCE_AUTO,
            },
            subentry_type=SUBENTRY_TYPE,
            title=calc.average_title(
                area.name if area else area_id, device_class
            ),
            unique_id=key,
        )
        hass.config_entries.async_add_subentry(entry, subentry)
        adopted.append(key)
        assignments.append((reg_entry.entity_id, subentry.subentry_id))

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, DATA_AUTO_CREATED: sorted(adopted)},
        options=options,
        version=CONFIG_VERSION,
    )

    # Only now that the subentries exist may entities point at them.
    for entity_id, subentry_id in assignments:
        ent_reg.async_update_entity(entity_id, config_subentry_id=subentry_id)

    _LOGGER.info(
        "Area Averages: adopted %d existing average(s) as subentries: %s",
        len(adopted),
        ", ".join(sorted(adopted)) or "none",
    )
