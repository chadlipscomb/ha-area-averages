"""Upgrading a version 1 entry must not disturb a working installation."""

from homeassistant.helpers import entity_registry as er

from custom_components.area_averages import calc
from custom_components.area_averages.const import (
    CONF_AREA_ID,
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_CLASSES,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_CLASSES,
    CONFIG_VERSION,
    DATA_AUTO_CREATED,
    DOMAIN,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry


def _v1_entry(hass, device_classes):
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Area Averages",
        version=1,
        data={},
        options={
            CONF_DEVICE_CLASSES: device_classes,
            "stale_hours": 24,
            "battery_threshold": 20,
            "trend_minutes": 120,
            "exclude_label": "exclude-average",
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


def _existing_average(hass, config_entry, area_id, device_class, object_id):
    """An average sensor as version 1 left it in the registry."""
    return er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        calc.average_unique_id(area_id, device_class),
        suggested_object_id=object_id,
        config_entry=config_entry,
    )


async def test_migration_keeps_the_entity_id_and_adopts_it_as_a_subentry(
    hass, area, add_sensor
):
    bathroom = area("Master Bathroom")
    add_sensor("sensor.bath_temp", bathroom, "20")
    config_entry = _v1_entry(hass, ["temperature"])
    before = _existing_average(
        hass, config_entry, bathroom, "temperature", "master_bathroom_average_temperature"
    )
    assert before.entity_id == "sensor.master_bathroom_average_temperature"

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.version == CONFIG_VERSION
    (subentry,) = list(config_entry.subentries.values())
    assert subentry.data[CONF_AREA_ID] == bathroom
    assert subentry.data[CONF_DEVICE_CLASS] == "temperature"
    assert subentry.title == "Master Bathroom Average Temperature"

    after = er.async_get(hass).async_get(before.entity_id)
    assert after is not None, "the migration must not orphan the existing entity"
    assert after.unique_id == before.unique_id
    assert after.config_subentry_id == subentry.subentry_id
    assert hass.states.get(before.entity_id).state == "20.0"


async def test_migration_turns_the_old_device_class_list_into_auto_discovery(
    hass, area, add_sensor
):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    add_sensor("sensor.kitchen_hum", kitchen, "50", device_class="humidity", unit="%")
    config_entry = _v1_entry(hass, ["temperature", "humidity"])
    _existing_average(
        hass, config_entry, kitchen, "temperature", "kitchen_average_temperature"
    )
    _existing_average(
        hass, config_entry, kitchen, "humidity", "kitchen_average_humidity"
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.options[CONF_AUTO_DISCOVER] is True
    assert config_entry.options[CONF_AUTO_DISCOVER_CLASSES] == [
        "temperature",
        "humidity",
    ]
    assert CONF_DEVICE_CLASSES not in config_entry.options
    assert len(config_entry.subentries) == 2
    assert sorted(config_entry.data[DATA_AUTO_CREATED]) == sorted(
        [
            calc.subentry_key(kitchen, "temperature"),
            calc.subentry_key(kitchen, "humidity"),
        ]
    )


async def test_migration_leaves_an_unrecognisable_average_alone(hass, area, add_sensor):
    """An average whose area was deleted cannot be matched; deleting it here
    would throw away real history."""
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = _v1_entry(hass, ["temperature"])
    _existing_average(
        hass, config_entry, kitchen, "temperature", "kitchen_average_temperature"
    )
    stranger = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "deleted_area_temperature_average",
        suggested_object_id="ghost_average_temperature",
        config_entry=config_entry,
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert len(config_entry.subentries) == 1
    assert er.async_get(hass).async_get(stranger.entity_id) is None, (
        "an average with no matching subentry is pruned, not left as unavailable"
    )


async def test_migration_of_an_entry_that_never_created_anything(hass):
    """No areas, no sensors, nothing in the registry: still a clean v2."""
    config_entry = _v1_entry(hass, ["temperature"])

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.version == CONFIG_VERSION
    assert config_entry.subentries == {}
    assert config_entry.options[CONF_AUTO_DISCOVER] is True
