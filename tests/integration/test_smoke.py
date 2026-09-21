"""The harness works and the integration loads."""

from homeassistant.config_entries import ConfigEntryState


async def test_entry_with_no_averages_loads_and_creates_no_sensors(hass, entry):
    config_entry = entry()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert [s for s in hass.states.async_entity_ids("sensor")] == []
    # The two reveal switches always exist.
    assert hass.states.get("switch.area_averages_show_offline") is not None
    assert hass.states.get("switch.area_averages_show_sensors") is not None
