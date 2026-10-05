"""Config, options and subentry flows for Area Averages.

There is one config entry holding the settings that apply to every average,
and one SUBENTRY per average. The subentry is what puts a row with a gear
icon on the integration page, so each area's average can be configured on
its own instead of through one global list.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.selector import (
    AreaSelector,
    AreaSelectorConfig,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    LabelSelector,
    LabelSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import calc
from .const import (
    CONF_AREA_ID,
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_CLASSES,
    CONF_BATTERY_THRESHOLD,
    CONF_DEVICE_CLASS,
    CONF_EXCLUDE_LABEL,
    CONF_IGNORED,
    CONF_INCLUDE_ENTITIES,
    CONF_INCLUDE_LABEL,
    CONF_MODE,
    CONF_SOURCE,
    CONF_STALE_HOURS,
    CONF_TREND_MINUTES,
    CONFIG_VERSION,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_BATTERY_THRESHOLD,
    DEFAULT_DEVICE_CLASSES,
    DEFAULT_EXCLUDE_LABEL,
    DEFAULT_STALE_HOURS,
    DEFAULT_TREND_MINUTES,
    DOCS_URL,
    DOMAIN,
    SOURCE_USER,
    SUBENTRY_TYPE,
    SUPPORTED_DEVICE_CLASSES,
)
from .registry import area_sensor_ids, resolve_label_ids


@callback
def _device_class_selector(multiple: bool) -> SelectSelector:
    """The supported device classes, translated by the frontend."""
    return SelectSelector(
        SelectSelectorConfig(
            options=SUPPORTED_DEVICE_CLASSES,
            multiple=multiple,
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="device_classes",
        )
    )


@callback
def _area_name(hass: Any, area_id: str) -> str:
    """Friendly area name, falling back to the id for a deleted area."""
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area is not None else area_id


class AreaAveragesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the (single-instance) config flow."""

    VERSION = CONFIG_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry; averages are added afterwards as subentries."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="Area Averages",
                data={},
                options={
                    CONF_AUTO_DISCOVER: user_input[CONF_AUTO_DISCOVER],
                    CONF_AUTO_DISCOVER_CLASSES: user_input[
                        CONF_AUTO_DISCOVER_CLASSES
                    ],
                    CONF_STALE_HOURS: DEFAULT_STALE_HOURS,
                    CONF_BATTERY_THRESHOLD: DEFAULT_BATTERY_THRESHOLD,
                    CONF_TREND_MINUTES: DEFAULT_TREND_MINUTES,
                    CONF_EXCLUDE_LABEL: DEFAULT_EXCLUDE_LABEL,
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AUTO_DISCOVER, default=DEFAULT_AUTO_DISCOVER
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_AUTO_DISCOVER_CLASSES,
                        default=list(DEFAULT_DEVICE_CLASSES),
                    ): _device_class_selector(multiple=True),
                }
            ),
            description_placeholders={"docs_url": DOCS_URL},
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """One average per subentry."""
        return {SUBENTRY_TYPE: AverageSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return AreaAveragesOptionsFlow()


class AreaAveragesOptionsFlow(OptionsFlow):
    """Settings shared by every average, plus auto-discovery."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the global options."""
        options = self.config_entry.options
        ignored = [str(key) for key in options.get(CONF_IGNORED, [])]

        if user_input is not None:
            # Anything not on the form must be carried through by hand: the
            # options flow replaces the whole options mapping.
            return self.async_create_entry(
                title="",
                data={**user_input, CONF_IGNORED: user_input.get(CONF_IGNORED, [])},
            )

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_AUTO_DISCOVER,
                default=bool(options.get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)),
            ): BooleanSelector(),
            vol.Required(
                CONF_AUTO_DISCOVER_CLASSES,
                default=list(
                    options.get(CONF_AUTO_DISCOVER_CLASSES, DEFAULT_DEVICE_CLASSES)
                ),
            ): _device_class_selector(multiple=True),
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

        if ignored:
            # Only rendered when there is something to un-ignore. Every
            # option is already selected; unticking one lets auto-discovery
            # create that average again.
            schema[vol.Optional(CONF_IGNORED, default=ignored)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=key, label=self._ignored_label(key)
                        )
                        for key in ignored
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))

    @callback
    def _ignored_label(self, key: str) -> str:
        """"kitchen|humidity" -> "Kitchen — Humidity"."""
        parsed = calc.parse_subentry_key(key)
        if parsed is None:
            return key
        area_id, device_class = parsed
        return f"{_area_name(self.hass, area_id)} — {calc.nice_name(device_class)}"

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


class AverageSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one average.

    Creating takes two steps -- which average, then which sensors go in it.
    Reconfiguring shows only the second: the area and the device class are
    what the average IS, and changing them would be a different sensor with
    a different history. Delete and re-add instead.
    """

    def __init__(self) -> None:
        self._area_id: str = ""
        self._device_class: str = ""

    # ------------------------------------------------------------ create

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the area and the kind of average."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._area_id = user_input[CONF_AREA_ID]
            self._device_class = user_input[CONF_DEVICE_CLASS]
            if self._existing_subentry(self._area_id, self._device_class):
                errors["base"] = "already_configured"
            else:
                return await self.async_step_members()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AREA_ID): AreaSelector(AreaSelectorConfig()),
                    vol.Required(
                        CONF_DEVICE_CLASS, default="temperature"
                    ): _device_class_selector(multiple=False),
                }
            ),
            errors=errors,
        )

    async def async_step_members(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick how the members of this average are chosen."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._title(),
                data=self._data(user_input, source=SOURCE_USER),
                unique_id=calc.subentry_key(self._area_id, self._device_class),
            )
        return self.async_show_form(
            step_id="members",
            data_schema=self._members_schema({}),
            description_placeholders={"average": self._title()},
        )

    # ------------------------------------------------------- reconfigure

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing average. Function name is fixed by core."""
        subentry = self._get_reconfigure_subentry()
        self._area_id = subentry.data[CONF_AREA_ID]
        self._device_class = subentry.data[CONF_DEVICE_CLASS]

        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=self._title(),
                data=self._data(
                    user_input, source=subentry.data.get(CONF_SOURCE, SOURCE_USER)
                ),
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._members_schema(subentry.data),
            description_placeholders={"average": self._title()},
        )

    # ----------------------------------------------------------- helpers

    @callback
    def _existing_subentry(self, area_id: str, device_class: str) -> bool:
        """True when this area already has an average of this kind."""
        wanted = calc.subentry_key(area_id, device_class)
        return any(
            subentry.unique_id == wanted
            or (
                subentry.data.get(CONF_AREA_ID) == area_id
                and subentry.data.get(CONF_DEVICE_CLASS) == device_class
            )
            for subentry in self._get_entry().subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE
        )

    @callback
    def _title(self) -> str:
        return calc.average_title(
            _area_name(self.hass, self._area_id), self._device_class
        )

    @callback
    def _data(self, user_input: dict[str, Any], source: str) -> dict[str, Any]:
        """Flow answers -> stored subentry data."""
        mode = user_input.get(CONF_MODE, calc.MODE_EXCLUDE)
        include_entities = list(user_input.get(CONF_INCLUDE_ENTITIES, []))
        include_label = user_input.get(CONF_INCLUDE_LABEL) or None
        return {
            CONF_AREA_ID: self._area_id,
            CONF_DEVICE_CLASS: self._device_class,
            CONF_MODE: mode,
            CONF_INCLUDE_ENTITIES: include_entities,
            CONF_INCLUDE_LABEL: include_label,
            CONF_SOURCE: source,
        }

    @callback
    def _members_schema(self, current: Any) -> vol.Schema:
        """The member-selection form, filled in from ``current``.

        The entity picker is restricted to sensors of this device class in
        this area, plus anything already stored -- a member that has since
        moved out of the area must stay visible, or reconfiguring would
        silently drop it.
        """
        stored_entities = [
            str(entity_id) for entity_id in current.get(CONF_INCLUDE_ENTITIES, [])
        ]
        offered = area_sensor_ids(self.hass, self._area_id, self._device_class)
        for entity_id in stored_entities:
            if entity_id not in offered:
                offered.append(entity_id)

        return vol.Schema(
            {
                vol.Required(
                    CONF_MODE, default=current.get(CONF_MODE, calc.MODE_EXCLUDE)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(calc.MODES),
                        mode=SelectSelectorMode.LIST,
                        translation_key="mode",
                    )
                ),
                vol.Optional(
                    CONF_INCLUDE_ENTITIES, default=stored_entities
                ): EntitySelector(
                    EntitySelectorConfig(
                        include_entities=sorted(offered), multiple=True
                    )
                ),
                vol.Optional(
                    CONF_INCLUDE_LABEL,
                    description={"suggested_value": current.get(CONF_INCLUDE_LABEL)},
                ): LabelSelector(LabelSelectorConfig(multiple=False)),
            }
        )
