"""Behaviour of a configured average, and of the global options."""

from datetime import timedelta

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, label_registry as lr
from homeassistant.util import dt as dt_util

from custom_components.area_averages import calc
from custom_components.area_averages.const import (
    CONF_AREA_ID,
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_CLASSES,
    CONF_BATTERY_THRESHOLD,
    CONF_DEVICE_CLASS,
    CONF_EXCLUDE_LABEL,
    CONF_INCLUDE_ENTITIES,
    CONF_INCLUDE_LABEL,
    CONF_MODE,
    CONF_SOURCE,
    CONF_STALE_HOURS,
    CONF_TREND_MINUTES,
    DOMAIN,
    SOURCE_USER,
)

from pytest_homeassistant_custom_component.common import async_fire_time_changed


async def _settle(hass):
    """Let the debounced membership refresh run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()


def _subentry(area_id, device_class="temperature", **data):
    return {
        "data": {
            CONF_AREA_ID: area_id,
            CONF_DEVICE_CLASS: device_class,
            CONF_MODE: calc.MODE_EXCLUDE,
            CONF_INCLUDE_ENTITIES: [],
            CONF_INCLUDE_LABEL: None,
            CONF_SOURCE: SOURCE_USER,
            **data,
        },
        "subentry_type": "average",
        "title": f"Average {device_class}",
        "unique_id": calc.subentry_key(area_id, device_class),
    }


async def test_an_average_with_no_members_stays_and_says_so(hass, entry, area):
    """It is explicit configuration now: it must not vanish silently."""
    spare = area("Spare Room")
    config_entry = entry(subentries_data=[_subentry(spare)])

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.spare_room_average_temperature")
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["member_count"] == 0
    assert state.attributes["summary"] == "no sensors reporting"
    assert state.attributes["healthy"] is False


async def test_a_sensor_moved_into_the_area_joins_an_exclude_mode_average(
    hass, entry, area, add_sensor
):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry(subentries_data=[_subentry(kitchen)])
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_average_temperature").state == "20.0"

    add_sensor("sensor.late_arrival", kitchen, "30")
    await hass.async_block_till_done()

    await _settle(hass)

    state = hass.states.get("sensor.kitchen_average_temperature")
    assert state.state == "25.0"
    assert state.attributes["member_count"] == 2


async def test_include_mode_ignores_a_sensor_added_to_the_area(
    hass, entry, area, add_sensor
):
    """The point of include mode: a fixed list that nothing can join."""
    office = area("Office")
    add_sensor("sensor.desk", office, "20")
    config_entry = entry(
        subentries_data=[
            _subentry(
                office,
                mode=calc.MODE_INCLUDE,
                include_entities=["sensor.desk"],
            )
        ]
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    add_sensor("sensor.new_probe", office, "40")
    await _settle(hass)

    state = hass.states.get("sensor.office_average_temperature")
    assert state.state == "20.0"
    assert state.attributes["sensors"] == ["sensor.desk"]
    assert state.attributes["excluded"] == ["sensor.new_probe"]


async def test_the_include_label_adds_a_sensor_without_editing_the_list(
    hass, entry, area, add_sensor
):
    label = lr.async_get(hass).async_create("in-the-average")
    office = area("Office")
    add_sensor("sensor.desk", office, "20")
    add_sensor("sensor.shelf", office, "30", labels={label.label_id})
    add_sensor("sensor.window", office, "40")
    config_entry = entry(
        subentries_data=[
            _subentry(
                office,
                mode=calc.MODE_INCLUDE,
                include_entities=["sensor.desk"],
                include_label=label.label_id,
            )
        ]
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.office_average_temperature")
    assert state.attributes["sensors"] == ["sensor.desk", "sensor.shelf"]
    assert state.state == "25.0"


async def test_a_diagnostic_sensor_is_neither_a_member_nor_listed_as_excluded(
    hass, entry, area, add_sensor
):
    """The dashboard lists `excluded` under "Not in average"; a radio chip
    temperature has no business appearing there."""
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    add_sensor(
        "sensor.radio_chip", kitchen, "45", entity_category=er.EntityCategory.DIAGNOSTIC
    )
    config_entry = entry(subentries_data=[_subentry(kitchen)])

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.kitchen_average_temperature")
    assert state.state == "20.0"
    assert state.attributes["sensors"] == ["sensor.kitchen_temp"]
    assert state.attributes["excluded"] == []


async def test_an_excluded_sensor_is_listed_but_not_counted(
    hass, entry, area, add_sensor
):
    label = lr.async_get(hass).async_create("exclude-average")
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    add_sensor("sensor.oven", kitchen, "200", labels={label.label_id})
    config_entry = entry(subentries_data=[_subentry(kitchen)])

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.kitchen_average_temperature")
    assert state.state == "20.0"
    assert state.attributes["excluded"] == ["sensor.oven"]


async def test_the_average_is_assigned_to_its_area(hass, entry, area, add_sensor):
    """So it shows on area cards and to voice assistants."""
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry(subentries_data=[_subentry(kitchen)])

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    reg_entry = er.async_get(hass).async_get("sensor.kitchen_average_temperature")
    assert reg_entry.area_id == kitchen


# ------------------------------------------------------------ config flow


async def test_the_initial_config_flow_creates_one_entry_with_no_averages(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_AUTO_DISCOVER: False, CONF_AUTO_DISCOVER_CLASSES: ["temperature"]},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    config_entry = result["result"]
    assert config_entry.options[CONF_AUTO_DISCOVER] is False
    assert config_entry.subentries == {}


async def test_only_one_entry_is_allowed(hass, entry):
    entry()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


# --------------------------------------------------------- options flow


async def test_the_options_flow_saves_the_shared_settings(hass, entry, area, add_sensor):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry(subentries_data=[_subentry(kitchen)])
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_AUTO_DISCOVER: False,
            CONF_AUTO_DISCOVER_CLASSES: ["temperature"],
            CONF_STALE_HOURS: 6,
            CONF_BATTERY_THRESHOLD: 15,
            CONF_TREND_MINUTES: 60,
            CONF_EXCLUDE_LABEL: "exclude-average",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_STALE_HOURS] == 6
    # The averages survive the reload the change triggers.
    assert hass.states.get("sensor.kitchen_average_temperature") is not None
    assert len(config_entry.subentries) == 1
