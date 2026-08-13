"""Sensor platform for Area Averages.

One average sensor is created per (area, device class). Membership is
resolved from the entity registry, so sensors added to an area are picked
up automatically, and anything carrying the exclude label (on the entity
or its device) is left out. Diagnostic/config-category entities (device
chip temps, radio temps, ...) are never included.

The average is the mean of the members that are currently reporting a
numeric state, so one live sensor is enough to keep the value meaningful.
The `issue` attribute is None when everything is healthy; otherwise it
lists members that are stale (no data inside the window) or low battery.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import fmean
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import PressureConverter, TemperatureConverter

from .const import (
    ATTR_EXCLUDED,
    ATTR_ISSUE,
    ATTR_ISSUES,
    ATTR_LOW_BATTERY,
    ATTR_MEMBER_COUNT,
    ATTR_REPORTING,
    ATTR_SENSORS,
    ATTR_STALE,
    CONF_BATTERY_THRESHOLD,
    CONF_DEVICE_CLASSES,
    CONF_EXCLUDE_LABEL,
    CONF_STALE_HOURS,
    DEFAULT_BATTERY_THRESHOLD,
    DEFAULT_DEVICE_CLASSES,
    DEFAULT_EXCLUDE_LABEL,
    DEFAULT_STALE_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

REFRESH_DEBOUNCE = 2.0  # seconds; coalesces registry-event bursts
PERIODIC_INTERVAL = timedelta(minutes=10)  # re-check staleness/battery

UNIT_CONVERTERS = {
    "temperature": TemperatureConverter,
    "pressure": PressureConverter,
}

NICE_NAMES = {
    "temperature": "Temperature",
    "humidity": "Humidity",
    "pressure": "Pressure",
    "carbon_dioxide": "CO2",
    "illuminance": "Illuminance",
    "pm25": "PM2.5",
}


@dataclass
class Membership:
    """Resolved membership of one (area, device class) bucket."""

    members: list[str] = field(default_factory=list)
    batteries: dict[str, list[str]] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)


@dataclass
class LastNumericData(ExtraStoredData):
    """Per-member 'last reported a number' timestamps, persisted so
    staleness detection survives HA restarts (state timestamps reset)."""

    last_numeric: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"last_numeric": self.last_numeric}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the platform and start the area manager."""
    manager = AreaAveragesManager(hass, entry, async_add_entities)
    await manager.async_start()


class AreaAveragesManager:
    """Keeps the set of average sensors in sync with the registries."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._async_add_entities = async_add_entities
        self._entities: dict[tuple[str, str], AreaAverageSensor] = {}
        self._pending_refresh: CALLBACK_TYPE | None = None
        self._exclude_label_ids: set[str] = set()

        options = entry.options
        self.device_classes: list[str] = list(
            options.get(CONF_DEVICE_CLASSES, DEFAULT_DEVICE_CLASSES)
        )
        self.stale_hours: float = float(
            options.get(CONF_STALE_HOURS, DEFAULT_STALE_HOURS)
        )
        self.battery_threshold: float = float(
            options.get(CONF_BATTERY_THRESHOLD, DEFAULT_BATTERY_THRESHOLD)
        )
        self.exclude_label: str = str(
            options.get(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL)
        )

    async def async_start(self) -> None:
        """Resolve initial membership and subscribe to registry changes."""
        self._resolve_exclude_label()

        for event_type in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            ar.EVENT_AREA_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            lr.EVENT_LABEL_REGISTRY_UPDATED,
        ):
            self.entry.async_on_unload(
                self.hass.bus.async_listen(event_type, self._handle_registry_event)
            )
        self.entry.async_on_unload(
            async_track_time_interval(self.hass, self._handle_periodic, PERIODIC_INTERVAL)
        )
        if not self.hass.is_running:
            self.entry.async_on_unload(
                self.hass.bus.async_listen(
                    EVENT_HOMEASSISTANT_STARTED, self._handle_periodic
                )
            )
        self.entry.async_on_unload(self._cancel_pending_refresh)
        self._refresh()

    # ------------------------------------------------------------- events

    @callback
    def _cancel_pending_refresh(self) -> None:
        if self._pending_refresh is not None:
            self._pending_refresh()
            self._pending_refresh = None

    @callback
    def _handle_registry_event(self, event: Event) -> None:
        if event.event_type == lr.EVENT_LABEL_REGISTRY_UPDATED:
            self._resolve_exclude_label()
        self._schedule_refresh()

    @callback
    def _schedule_refresh(self) -> None:
        if self._pending_refresh is not None:
            return
        self._pending_refresh = async_call_later(
            self.hass, REFRESH_DEBOUNCE, self._run_refresh
        )

    @callback
    def _run_refresh(self, _now: Any) -> None:
        self._pending_refresh = None
        self._refresh()

    @callback
    def _handle_periodic(self, _arg: Any = None) -> None:
        for entity in self._entities.values():
            entity.async_refresh_state()

    # --------------------------------------------------------- membership

    @callback
    def _resolve_exclude_label(self) -> None:
        """Map the configured label name to label registry ids."""
        wanted = self.exclude_label.strip().lower()
        ids: set[str] = set()
        for label in lr.async_get(self.hass).labels.values():
            if label.name.strip().lower() == wanted or label.label_id == slugify(wanted):
                ids.add(label.label_id)
        self._exclude_label_ids = ids

    @callback
    def _battery_entities(
        self, ent_reg: er.EntityRegistry, device_id: str
    ) -> list[str]:
        """Battery level / low-battery entities that live on the same device."""
        found: list[str] = []
        for reg_entry in er.async_entries_for_device(ent_reg, device_id):
            if reg_entry.domain not in ("sensor", "binary_sensor"):
                continue
            if (reg_entry.device_class or reg_entry.original_device_class) == "battery":
                found.append(reg_entry.entity_id)
        return found

    @callback
    def _collect(self) -> dict[tuple[str, str], Membership]:
        """Resolve membership for every (area, device class) bucket."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        wanted = set(self.device_classes)
        buckets: dict[tuple[str, str], Membership] = {}

        for reg_entry in list(ent_reg.entities.values()):
            if reg_entry.domain != "sensor" or reg_entry.platform == DOMAIN:
                continue
            if reg_entry.disabled_by is not None:
                continue
            if reg_entry.entity_category is not None:
                continue
            device_class = reg_entry.device_class or reg_entry.original_device_class
            if device_class not in wanted:
                continue

            device = (
                dev_reg.async_get(reg_entry.device_id)
                if reg_entry.device_id
                else None
            )
            area_id = reg_entry.area_id or (device.area_id if device else None)
            if not area_id:
                continue

            labels = set(reg_entry.labels)
            if device:
                labels |= set(device.labels)

            bucket = buckets.setdefault((area_id, device_class), Membership())
            if labels & self._exclude_label_ids:
                bucket.excluded.append(reg_entry.entity_id)
                continue
            bucket.members.append(reg_entry.entity_id)
            if reg_entry.device_id:
                batteries = self._battery_entities(ent_reg, reg_entry.device_id)
                if batteries:
                    bucket.batteries[reg_entry.entity_id] = batteries

        for bucket in buckets.values():
            bucket.members.sort()
            bucket.excluded.sort()
        return buckets

    @callback
    def _refresh(self) -> None:
        """Create/update/remove average sensors to match the registries."""
        buckets = self._collect()
        desired = {key: value for key, value in buckets.items() if value.members}
        area_reg = ar.async_get(self.hass)

        new_entities: list[AreaAverageSensor] = []
        for key, membership in desired.items():
            if key in self._entities:
                self._entities[key].async_set_membership(membership)
                continue
            if area_reg.async_get_area(key[0]) is None:
                continue
            sensor = AreaAverageSensor(self, key[0], key[1], membership)
            self._entities[key] = sensor
            new_entities.append(sensor)

        for key in [k for k in self._entities if k not in desired]:
            entity = self._entities.pop(key)
            self.hass.async_create_task(self._async_remove_entity(entity))

        if new_entities:
            self._async_add_entities(new_entities)

    async def _async_remove_entity(self, entity: AreaAverageSensor) -> None:
        """Remove an average whose area lost all members (or was deleted)."""
        entity_id = entity.entity_id
        await entity.async_remove(force_remove=True)
        ent_reg = er.async_get(self.hass)
        if entity_id and ent_reg.async_get(entity_id) is not None:
            ent_reg.async_remove(entity_id)


class AreaAverageSensor(SensorEntity, RestoreEntity):
    """Mean of the reporting sensors of one device class in one area."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({ATTR_SENSORS, ATTR_EXCLUDED})

    def __init__(
        self,
        manager: AreaAveragesManager,
        area_id: str,
        device_class: str,
        membership: Membership,
    ) -> None:
        self._manager = manager
        self._area_id = area_id
        self._device_class_key = device_class
        self._membership = membership
        self._unsub_state: CALLBACK_TYPE | None = None
        self._last_numeric: dict[str, Any] = {}

        self._attr_unique_id = f"{area_id}_{device_class}_average"
        try:
            self._attr_device_class = SensorDeviceClass(device_class)
        except ValueError:
            self._attr_device_class = None
        if device_class == "temperature":
            self._attr_native_unit_of_measurement = (
                manager.hass.config.units.temperature_unit
            )
        elif device_class == "humidity":
            self._attr_native_unit_of_measurement = "%"
        else:
            # Adopted from the first reporting member.
            self._attr_native_unit_of_measurement = None
        self._attr_suggested_display_precision = 1
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._refresh_name()

    # ------------------------------------------------------------ helpers

    @callback
    def _refresh_name(self) -> None:
        area = ar.async_get(self._manager.hass).async_get_area(self._area_id)
        area_name = area.name if area else self._area_id
        nice = NICE_NAMES.get(
            self._device_class_key,
            self._device_class_key.replace("_", " ").title(),
        )
        self._attr_name = f"{area_name} Average {nice}"

    @staticmethod
    def _numeric_value(state: State) -> float | None:
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return value

    def _converted(self, value: float, unit: str | None) -> float | None:
        """Convert a member value into this sensor's unit if needed."""
        target = self._attr_native_unit_of_measurement
        if target is None:
            self._attr_native_unit_of_measurement = unit
            return value
        if unit is None or unit == target:
            return value
        converter = UNIT_CONVERTERS.get(self._device_class_key)
        if (
            converter is not None
            and unit in converter.VALID_UNITS
            and target in converter.VALID_UNITS
        ):
            return converter.convert(value, unit, target)
        _LOGGER.debug(
            "%s: skipping value %s %s (average unit is %s)",
            self.entity_id,
            value,
            unit,
            target,
        )
        return None

    # ---------------------------------------------------------- lifecycle

    @property
    def extra_restore_state_data(self) -> LastNumericData:
        """Persist last-numeric timestamps across restarts."""
        return LastNumericData(
            {k: v.isoformat() for k, v in self._last_numeric.items()}
        )

    async def async_added_to_hass(self) -> None:
        """Restore staleness clocks, subscribe, place the entity in its area."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            for entity_id, iso in (extra.as_dict().get("last_numeric") or {}).items():
                parsed = (
                    dt_util.parse_datetime(iso) if isinstance(iso, str) else None
                )
                if parsed is not None:
                    self._last_numeric[entity_id] = parsed
        self._subscribe()
        ent_reg = er.async_get(self.hass)
        reg_entry = ent_reg.async_get(self.entity_id)
        if reg_entry is not None and reg_entry.area_id is None:
            ent_reg.async_update_entity(self.entity_id, area_id=self._area_id)
        self._recompute()

    async def async_will_remove_from_hass(self) -> None:
        self._unsubscribe()

    @callback
    def _unsubscribe(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def _subscribe(self) -> None:
        self._unsubscribe()
        tracked = set(self._membership.members)
        for batteries in self._membership.batteries.values():
            tracked.update(batteries)
        if tracked:
            self._unsub_state = async_track_state_change_event(
                self.hass, sorted(tracked), self._handle_member_change
            )

    @callback
    def _handle_member_change(self, _event: Event) -> None:
        self._recompute()

    @callback
    def async_set_membership(self, membership: Membership) -> None:
        """Called by the manager whenever registries change."""
        self._membership = membership
        current = set(membership.members)
        self._last_numeric = {
            k: v for k, v in self._last_numeric.items() if k in current
        }
        self._refresh_name()
        if self.hass is None:
            return
        self._subscribe()
        self._recompute()

    @callback
    def async_refresh_state(self) -> None:
        """Periodic re-evaluation so staleness/battery age out on time."""
        if self.hass is not None:
            self._recompute()

    # -------------------------------------------------------------- state

    @callback
    def _recompute(self) -> None:
        now = dt_util.utcnow()
        stale_cutoff = timedelta(hours=self._manager.stale_hours)
        values: list[float] = []
        issues: list[str] = []
        stale: list[str] = []
        low_battery: list[str] = []

        for entity_id in self._membership.members:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            value = self._numeric_value(state)
            if value is not None:
                converted = self._converted(
                    value, state.attributes.get("unit_of_measurement")
                )
                if converted is not None:
                    values.append(converted)
                # Time of the last actual report, even if the value repeated.
                last = state.last_reported or state.last_updated
                self._last_numeric[entity_id] = last
            else:
                # Not reporting. Prefer the persisted last-numeric time; the
                # live last_changed resets on every HA restart.
                last = self._last_numeric.get(entity_id) or state.last_changed
            age = now - last
            if age >= stale_cutoff:
                stale.append(entity_id)
                issues.append(
                    f"{state.name}: no data for {int(age.total_seconds() // 3600)} h"
                )

        battery_owner: dict[str, str] = {}
        for member, batteries in self._membership.batteries.items():
            for battery_id in batteries:
                battery_owner.setdefault(battery_id, member)
        for battery_id in sorted(battery_owner):
            state = self.hass.states.get(battery_id)
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                continue
            if battery_id.startswith("binary_sensor."):
                if state.state == "on":
                    low_battery.append(battery_id)
                    issues.append(f"{state.name}: low battery")
                continue
            try:
                level = float(state.state)
            except (TypeError, ValueError):
                continue
            if level <= self._manager.battery_threshold:
                low_battery.append(battery_id)
                issues.append(f"{state.name}: battery {round(level)}%")

        self._attr_native_value = round(fmean(values), 2) if values else None
        self._attr_extra_state_attributes = {
            ATTR_ISSUE: "; ".join(issues) if issues else None,
            ATTR_ISSUES: issues,
            ATTR_SENSORS: list(self._membership.members),
            ATTR_EXCLUDED: list(self._membership.excluded),
            ATTR_MEMBER_COUNT: len(self._membership.members),
            ATTR_REPORTING: len(values),
            ATTR_STALE: stale,
            ATTR_LOW_BATTERY: low_battery,
        }
        self.async_write_ha_state()
