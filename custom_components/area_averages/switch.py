"""The show-offline control, owned by the integration.

The dashboard needs somewhere to hang "reveal the sensors that are dark".
It used to point at a hand-made input_boolean that nothing in this repo
creates, so a fresh install silently lost the reveal branch of every
visibility condition. This switch is created with the config entry, so it
always exists; the strategy prefers it and falls back to the old helper.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import ENTITY_ID_FORMAT, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

#: The frontend resolves this exact entity_id first.
OBJECT_ID = "area_averages_show_offline"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the single show-offline switch."""
    async_add_entities([ShowOfflineSwitch(entry)])


class ShowOfflineSwitch(SwitchEntity, RestoreEntity):
    """Dashboard-only toggle: show members that are not reporting."""

    _attr_should_poll = False
    _attr_name = "Show offline sensors"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry) -> None:
        # Pre-setting entity_id makes it the suggested object id, so the
        # registry lands on switch.area_averages_show_offline rather than
        # slugifying the friendly name.
        self.entity_id = ENTITY_ID_FORMAT.format(OBJECT_ID)
        self._attr_unique_id = f"{entry.entry_id}_show_offline"
        self._attr_is_on = False

    @property
    def icon(self) -> str:
        return "mdi:eye-off-outline" if self.is_on else "mdi:lan-disconnect"

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
