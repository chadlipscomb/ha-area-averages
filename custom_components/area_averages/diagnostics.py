"""Diagnostics for Area Averages.

Answers the questions that were previously only answerable by reading the
source: which label ids did the exclude label actually resolve to, which
members are in which bucket, and when did each one last produce a number.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    manager = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if manager is None:
        return {
            "entry_options": dict(entry.options),
            "error": "the sensor platform is not loaded for this entry",
        }
    data = manager.diagnostics()
    data["entry_options"] = dict(entry.options)
    return data
