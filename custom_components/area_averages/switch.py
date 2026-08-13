"""The dashboard's two reveal controls, owned by the integration.

Each room section on the dashboard has two buttons: one that shows every
member sensor, one that shows only the members that are dark. They hang off
these switches. They used to point at a hand-made input_boolean that nothing
in this repo creates, so a fresh install silently lost the reveal branch of
every visibility condition. These are created with the config entry, so they
always exist; the strategy prefers them and falls back to the old helper.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import ENTITY_ID_FORMAT, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

#: The frontend resolves these exact entity_ids first.
OBJECT_ID = "area_averages_show_offline"
SENSORS_OBJECT_ID = "area_averages_show_sensors"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the two reveal switches."""
    async_add_entities([ShowOfflineSwitch(entry), ShowSensorsSwitch(entry)])


class RevealSwitch(SwitchEntity, RestoreEntity):
    """Dashboard-only toggle. Holds no state beyond on/off."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    #: Subclasses set these.
    _object_id = ""
    _unique_suffix = ""
    _icon_on = ""
    _icon_off = ""

    def __init__(self, entry: ConfigEntry) -> None:
        # Pre-setting entity_id makes it the suggested object id, so the
        # registry lands on the exact id the dashboard looks for rather than
        # slugifying the friendly name.
        self.entity_id = ENTITY_ID_FORMAT.format(self._object_id)
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_suffix}"
        self._attr_is_on = False

    @property
    def icon(self) -> str:
        return self._icon_on if self.is_on else self._icon_off

    async def async_added_to_hass(self) -> None:
        """Restore the last position across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self._attr_is_on = last_state.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class ShowOfflineSwitch(RevealSwitch):
    """Show the members that are not reporting."""

    _attr_name = "Show offline sensors"
    _object_id = OBJECT_ID
    _unique_suffix = "show_offline"
    _icon_on = "mdi:eye-off-outline"
    _icon_off = "mdi:lan-disconnect"


class ShowSensorsSwitch(RevealSwitch):
    """Show every member sensor behind each room's average."""

    _attr_name = "Show all sensors"
    _object_id = SENSORS_OBJECT_ID
    _unique_suffix = "show_sensors"
    _icon_on = "mdi:eye-off-outline"
    _icon_off = "mdi:format-list-bulleted"
