"""Adding and editing an average from the integration page."""

from homeassistant.data_entry_flow import FlowResultType

from custom_components.area_averages import calc
from custom_components.area_averages.const import (
    CONF_AREA_ID,
    CONF_DEVICE_CLASS,
    CONF_INCLUDE_ENTITIES,
    CONF_MODE,
    CONF_SOURCE,
    SOURCE_USER,
    SUBENTRY_TYPE,
)


async def _add_average(hass, config_entry, area_id, device_class, members=None):
    """Drive the two-step add flow; returns the final flow result."""
    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_TYPE), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_AREA_ID: area_id, CONF_DEVICE_CLASS: device_class},
    )
    if result["type"] is not FlowResultType.FORM:
        return result
    assert result["step_id"] == "members"

    payload = {CONF_MODE: calc.MODE_EXCLUDE}
    if members is not None:
        payload = {CONF_MODE: calc.MODE_INCLUDE, CONF_INCLUDE_ENTITIES: members}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], payload
    )
    await hass.async_block_till_done()
    return result


async def test_adding_an_average_creates_one_sensor_named_after_the_area(
    hass, entry, area, add_sensor
):
    bathroom = area("Master Bathroom")
    add_sensor("sensor.shower", bathroom, "21", device_class="humidity", unit="%")
    add_sensor("sensor.vanity", bathroom, "23", device_class="humidity", unit="%")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await _add_average(hass, config_entry, bathroom, "humidity")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Master Bathroom Average Humidity"

    state = hass.states.get("sensor.master_bathroom_average_humidity")
    assert state is not None
    assert state.state == "22.0"
    assert state.attributes["member_count"] == 2
    assert state.attributes["summary"] == "2 sensors"


async def test_the_subentry_carries_its_area_type_and_mode(hass, entry, area, add_sensor):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(hass, config_entry, kitchen, "temperature")

    (subentry,) = list(config_entry.subentries.values())
    assert subentry.subentry_type == SUBENTRY_TYPE
    assert subentry.unique_id == calc.subentry_key(kitchen, "temperature")
    assert subentry.data[CONF_AREA_ID] == kitchen
    assert subentry.data[CONF_DEVICE_CLASS] == "temperature"
    assert subentry.data[CONF_MODE] == calc.MODE_EXCLUDE
    assert subentry.data[CONF_SOURCE] == SOURCE_USER


async def test_the_entity_is_attached_to_its_own_subentry_row(
    hass, entry, area, add_sensor
):
    """This is what puts the sensor under that row on the integration page."""
    from homeassistant.helpers import entity_registry as er

    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(hass, config_entry, kitchen, "temperature")
    (subentry,) = list(config_entry.subentries.values())

    reg_entry = er.async_get(hass).async_get("sensor.kitchen_average_temperature")
    assert reg_entry is not None
    assert reg_entry.config_subentry_id == subentry.subentry_id


async def test_a_second_average_of_the_same_kind_is_refused(hass, entry, area, add_sensor):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(hass, config_entry, kitchen, "temperature")

    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_TYPE), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_AREA_ID: kitchen, CONF_DEVICE_CLASS: "temperature"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "already_configured"}
    assert len(config_entry.subentries) == 1


async def test_the_same_area_can_have_averages_of_different_kinds(
    hass, entry, area, add_sensor
):
    bathroom = area("Master Bathroom")
    add_sensor("sensor.bath_temp", bathroom, "20")
    add_sensor("sensor.bath_hum", bathroom, "55", device_class="humidity", unit="%")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(hass, config_entry, bathroom, "temperature")
    await _add_average(hass, config_entry, bathroom, "humidity")

    assert len(config_entry.subentries) == 2
    assert hass.states.get("sensor.master_bathroom_average_temperature") is not None
    assert hass.states.get("sensor.master_bathroom_average_humidity") is not None


async def test_include_mode_counts_only_the_picked_sensors(hass, entry, area, add_sensor):
    office = area("Office")
    add_sensor("sensor.desk", office, "20")
    add_sensor("sensor.window", office, "30")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(
        hass, config_entry, office, "temperature", members=["sensor.desk"]
    )

    state = hass.states.get("sensor.office_average_temperature")
    assert state.state == "20.0"
    assert state.attributes["sensors"] == ["sensor.desk"]
    assert state.attributes["excluded"] == ["sensor.window"]


async def test_reconfigure_switches_an_average_to_include_mode(
    hass, entry, area, add_sensor
):
    office = area("Office")
    add_sensor("sensor.desk", office, "20")
    add_sensor("sensor.window", office, "30")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(hass, config_entry, office, "temperature")
    assert hass.states.get("sensor.office_average_temperature").state == "25.0"
    (subentry,) = list(config_entry.subentries.values())

    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": subentry.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_MODE: calc.MODE_INCLUDE, CONF_INCLUDE_ENTITIES: ["sensor.desk"]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    state = hass.states.get("sensor.office_average_temperature")
    assert state.state == "20.0"
    assert state.attributes["sensors"] == ["sensor.desk"]


async def test_reconfigure_does_not_disturb_the_other_averages(
    hass, entry, area, add_sensor
):
    """Editing one average must not tear down the rest of the house."""
    office = area("Office")
    kitchen = area("Kitchen")
    add_sensor("sensor.desk", office, "20")
    add_sensor("sensor.kitchen_temp", kitchen, "18")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(hass, config_entry, office, "temperature")
    await _add_average(hass, config_entry, kitchen, "temperature")
    untouched = hass.states.get("sensor.kitchen_average_temperature")

    office_subentry = next(
        s for s in config_entry.subentries.values() if s.data[CONF_AREA_ID] == office
    )
    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": office_subentry.subentry_id},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_MODE: calc.MODE_EXCLUDE}
    )
    await hass.async_block_till_done()

    after = hass.states.get("sensor.kitchen_average_temperature")
    assert after.last_changed == untouched.last_changed


async def test_deleting_an_average_removes_its_sensor(hass, entry, area, add_sensor):
    kitchen = area("Kitchen")
    add_sensor("sensor.kitchen_temp", kitchen, "20")
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await _add_average(hass, config_entry, kitchen, "temperature")
    assert hass.states.get("sensor.kitchen_average_temperature") is not None
    (subentry,) = list(config_entry.subentries.values())

    hass.config_entries.async_remove_subentry(config_entry, subentry.subentry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.kitchen_average_temperature") is None
    assert config_entry.subentries == {}
