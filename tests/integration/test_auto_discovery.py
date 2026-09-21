"""Auto-discovery materialises averages as real, editable rows."""

from datetime import timedelta

from homeassistant.helpers import label_registry as lr
from homeassistant.util import dt as dt_util

from custom_components.area_averages import calc
from custom_components.area_averages.const import (
    CONF_AREA_ID,
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_CLASSES,
    CONF_IGNORED,
    CONF_SOURCE,
    DATA_AUTO_CREATED,
    SOURCE_AUTO,
)

from pytest_homeassistant_custom_component.common import async_fire_time_changed

DISCOVER = {CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_CLASSES: ["temperature"]}


async def _settle(hass):
    """Let the debounced pass run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()


async def test_discovery_creates_one_subentry_per_area_that_has_sensors(
    hass, entry, area, add_sensor
):
    kitchen = area("Kitchen")
    office = area("Office")
    area("Empty Room")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    add_sensor("sensor.desk", office, "22")
    config_entry = entry(options=DISCOVER)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    keys = {s.unique_id for s in config_entry.subentries.values()}
    assert keys == {
        calc.subentry_key(kitchen, "temperature"),
        calc.subentry_key(office, "temperature"),
    }, "an area with no sensors of that type gets no average"
    assert hass.states.get("sensor.kitchen_average_temperature").state == "20.0"
    assert hass.states.get("sensor.office_average_temperature").state == "22.0"
    assert all(
        s.data[CONF_SOURCE] == SOURCE_AUTO for s in config_entry.subentries.values()
    )


async def test_discovery_only_looks_at_the_configured_types(
    hass, entry, area, add_sensor
):
    bathroom = area("Master Bathroom")
    add_sensor("sensor.bath_temp", bathroom, "20")
    add_sensor("sensor.bath_hum", bathroom, "55", device_class="humidity", unit="%")
    config_entry = entry(options=DISCOVER)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.master_bathroom_average_temperature") is not None
    assert hass.states.get("sensor.master_bathroom_average_humidity") is None


async def test_discovery_skips_an_area_whose_sensors_are_all_excluded(
    hass, entry, area, add_sensor
):
    label = lr.async_get(hass).async_create("exclude-average")
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20", labels={label.label_id})
    config_entry = entry(options=DISCOVER)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.subentries == {}


async def test_a_discovered_average_that_is_deleted_stays_deleted(
    hass, entry, area, add_sensor
):
    kitchen = area("Kitchen")
    office = area("Office")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    add_sensor("sensor.desk", office, "22")
    config_entry = entry(options=DISCOVER)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(config_entry.subentries) == 2

    kitchen_key = calc.subentry_key(kitchen, "temperature")
    victim = next(
        s for s in config_entry.subentries.values() if s.unique_id == kitchen_key
    )
    hass.config_entries.async_remove_subentry(config_entry, victim.subentry_id)
    await _settle(hass)

    assert kitchen_key in config_entry.options[CONF_IGNORED]
    assert kitchen_key not in config_entry.data[DATA_AUTO_CREATED]
    assert hass.states.get("sensor.kitchen_average_temperature") is None
    assert len(config_entry.subentries) == 1, "discovery must not put it back"

    # And it stays gone across a restart.
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(config_entry.subentries) == 1
    assert hass.states.get("sensor.office_average_temperature") is not None


async def test_un_ignoring_an_average_lets_discovery_create_it_again(
    hass, entry, area, add_sensor
):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    key = calc.subentry_key(kitchen, "temperature")
    config_entry = entry(options={**DISCOVER, CONF_IGNORED: [key]})

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.subentries == {}

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_IGNORED: []}
    )
    await _settle(hass)

    assert len(config_entry.subentries) == 1
    assert hass.states.get("sensor.kitchen_average_temperature") is not None


async def test_turning_discovery_off_keeps_the_averages_it_made(
    hass, entry, area, add_sensor
):
    """Nothing is ever deleted behind the user's back."""
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry(options=DISCOVER)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(config_entry.subentries) == 1

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_AUTO_DISCOVER: False}
    )
    await hass.async_block_till_done()

    assert len(config_entry.subentries) == 1
    assert hass.states.get("sensor.kitchen_average_temperature") is not None


async def test_a_new_area_is_discovered_without_a_restart(hass, entry, area, add_sensor):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry(options=DISCOVER)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(config_entry.subentries) == 1

    garage = area("Garage")
    add_sensor("sensor.garage_temp", garage, "5")
    await _settle(hass)

    assert len(config_entry.subentries) == 2
    assert hass.states.get("sensor.garage_average_temperature").state == "5.0"


async def test_manual_averages_are_never_recorded_as_auto_created(
    hass, entry, area, add_sensor
):
    """Deleting an average you added by hand must not fill the ignore list."""
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry(
        options={CONF_AUTO_DISCOVER: False},
        subentries_data=[
            {
                "data": {
                    CONF_AREA_ID: kitchen,
                    "device_class": "temperature",
                    "mode": calc.MODE_EXCLUDE,
                    "include_entities": [],
                    "include_label": None,
                    CONF_SOURCE: "user",
                },
                "subentry_type": "average",
                "title": "Kitchen Average Temperature",
                "unique_id": calc.subentry_key(kitchen, "temperature"),
            }
        ],
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_average_temperature") is not None

    (subentry,) = list(config_entry.subentries.values())
    hass.config_entries.async_remove_subentry(config_entry, subentry.subentry_id)
    await _settle(hass)

    assert config_entry.options.get(CONF_IGNORED, []) == []
