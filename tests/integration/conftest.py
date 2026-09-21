"""Fixtures for the tests that run a real Home Assistant.

These need pytest-homeassistant-custom-component; the pure calc tests
deliberately do not, so they live one directory up.
"""

import pytest
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.area_averages.const import (
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_CLASSES,
    CONF_BATTERY_THRESHOLD,
    CONF_EXCLUDE_LABEL,
    CONF_STALE_HOURS,
    CONF_TREND_MINUTES,
    CONFIG_VERSION,
    DOMAIN,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry

BASE_OPTIONS = {
    CONF_AUTO_DISCOVER: False,
    CONF_AUTO_DISCOVER_CLASSES: ["temperature"],
    CONF_STALE_HOURS: 24,
    CONF_BATTERY_THRESHOLD: 20,
    CONF_TREND_MINUTES: 120,
    CONF_EXCLUDE_LABEL: "exclude-average",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/area_averages."""
    return


@pytest.fixture
def area(hass):
    """Create an area and return its id."""

    def _area(name):
        return ar.async_get(hass).async_get_or_create(name).id

    return _area


@pytest.fixture
def add_sensor(hass):
    """Register a sensor in an area and give it a state."""

    def _add(entity_id, area_id, value, device_class="temperature", unit="°C",
             entity_category=None, labels=None):
        object_id = entity_id.split(".", 1)[1]
        reg = er.async_get(hass)
        reg.async_get_or_create(
            "sensor",
            "demo",
            f"unique-{object_id}",
            suggested_object_id=object_id,
            original_device_class=device_class,
            entity_category=entity_category,
        )
        reg.async_update_entity(entity_id, area_id=area_id)
        if labels:
            reg.async_update_entity(entity_id, labels=set(labels))
        hass.states.async_set(
            entity_id,
            value,
            {"device_class": device_class, "unit_of_measurement": unit},
        )
        return entity_id

    return _add


@pytest.fixture
def entry(hass):
    """A loaded version-2 config entry with no averages yet."""

    def _entry(options=None, subentries_data=None, data=None):
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            title="Area Averages",
            version=CONFIG_VERSION,
            data=data or {},
            options={**BASE_OPTIONS, **(options or {})},
            subentries_data=subentries_data or [],
        )
        config_entry.add_to_hass(hass)
        return config_entry

    return _entry
