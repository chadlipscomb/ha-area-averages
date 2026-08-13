"""Config flow for Area Averages."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    LabelSelector,
    LabelSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BATTERY_THRESHOLD,
    CONF_DEVICE_CLASSES,
    CONF_EXCLUDE_LABEL,
    CONF_STALE_HOURS,
    CONF_TREND_MINUTES,
    DEFAULT_BATTERY_THRESHOLD,
    DEFAULT_DEVICE_CLASSES,
    DEFAULT_EXCLUDE_LABEL,
    DEFAULT_STALE_HOURS,
    DEFAULT_TREND_MINUTES,
    DOMAIN,
    SUPPORTED_DEVICE_CLASSES,
)
from .sensor import resolve_label_ids


class AreaAveragesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the (single-instance) config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry immediately; all tuning lives in options."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_create_entry(title="Area Averages", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return AreaAveragesOptionsFlow()


class AreaAveragesOptionsFlow(OptionsFlow):
    """Options: device classes, staleness window, battery threshold, label."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_CLASSES,
                    default=list(
                        options.get(CONF_DEVICE_CLASSES, DEFAULT_DEVICE_CLASSES)
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=SUPPORTED_DEVICE_CLASSES,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="device_classes",
                    )
                ),
                vol.Required(
                    CONF_STALE_HOURS,
                    default=options.get(CONF_STALE_HOURS, DEFAULT_STALE_HOURS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=168, step=1, unit_of_measurement="h",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_BATTERY_THRESHOLD,
                    default=options.get(
                        CONF_BATTERY_THRESHOLD, DEFAULT_BATTERY_THRESHOLD
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=100, step=1, unit_of_measurement="%",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_TREND_MINUTES,
                    default=options.get(CONF_TREND_MINUTES, DEFAULT_TREND_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=15, max=720, step=5, unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                # A picker, not free text: a typo here used to make every
                # exclusion silently vanish.
                vol.Required(
                    CONF_EXCLUDE_LABEL,
                    default=self._current_label_id(),
                ): LabelSelector(LabelSelectorConfig(multiple=False)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    @callback
    def _current_label_id(self) -> str:
        """The stored value as a real label_id, so upgrades keep their label.

        The option used to be free text ("exclude-average"); the picker
        wants the registry id ("exclude_average"). Resolve it the same way
        the sensor does rather than showing an empty picker.
        """
        stored = str(
            self.config_entry.options.get(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL)
        )
        matched = sorted(resolve_label_ids(self.hass, stored))
        return matched[0] if matched else stored
