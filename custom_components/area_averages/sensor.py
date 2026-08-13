"""Sensor platform for Area Averages.

One average sensor is created per (area, device class). Membership is
resolved from the entity registry, so sensors added to an area are picked
up automatically, and anything carrying the exclude label (on the entity
or its device) is left out. Diagnostic/config-category entities (device
chip temps, radio temps, ...) are never included.

The average is the mean of the members that are actually contributing a
number right now. Members are always in exactly one of three buckets:

  offline    no usable number at all (missing, unavailable, non-numeric,
             or an unconvertible unit). Immediate -- there is nothing to
             wait for -- and it wins over staleness.
  stale      holds a number, but has not reported inside the staleness
             window. Its value is DROPPED from the mean.
  reporting  everything else; these are the values inside the mean.

len(offline) + len(stale) + reporting == member_count, always.

The semantics of every published attribute live in calc.py, which is pure
stdlib and shared with tools/make_fixture.py so the test fixture cannot
drift away from what the sensor really does.
"""

from __future__ import annotations

import logging
import math
from collections import deque
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

from . import calc
from .const import (
    ATTR_EXCLUDED,
    ATTR_HEALTHY,
    ATTR_ISSUE,
    ATTR_ISSUES,
    ATTR_LOW_BATTERY,
    ATTR_MAXIMUM,
    ATTR_MEMBER_COUNT,
    ATTR_MINIMUM,
    ATTR_OFFLINE,
    ATTR_REPORTING,
    ATTR_SENSORS,
    ATTR_SPREAD,
    ATTR_STALE,
    ATTR_SUMMARY,
    ATTR_TREND,
    ATTR_TREND_RATE,
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

_LOGGER = logging.getLogger(__name__)

REFRESH_DEBOUNCE = 2.0  # seconds; coalesces registry-event bursts
PERIODIC_INTERVAL = timedelta(minutes=10)  # re-check staleness/battery/trend

#: Bounded, in-memory only. Never persisted, never read from the recorder.
TREND_MAX_SAMPLES = 90
TREND_MIN_SAMPLE_GAP = 60.0  # seconds between kept trend samples

TREND_MINUTES_MIN = 15
TREND_MINUTES_MAX = 720

UNIT_CONVERTERS: dict[str, Any] = {
    "temperature": TemperatureConverter,
    "pressure": PressureConverter,
}

try:  # available in newer cores; purely additive when it is
    from homeassistant.util.unit_conversion import (  # noqa: I001
        MassVolumeConcentrationConverter,
    )

    UNIT_CONVERTERS["pm25"] = MassVolumeConcentrationConverter
except ImportError:  # pragma: no cover - depends on core version
    pass

NICE_NAMES = {
    "temperature": "Temperature",
    "humidity": "Humidity",
    "pressure": "Pressure",
    "carbon_dioxide": "CO2",
    "illuminance": "Illuminance",
    "pm25": "PM2.5",
}


@callback
def resolve_label_ids(hass: HomeAssistant, configured: str) -> set[str]:
    """Label registry ids matching a configured label id, name or slug.

    Deliberately forgiving: the option used to be free text, so an entry
    saved as "exclude-average" must keep working after the picker lands.
    Shared with the options flow so both agree on what a stored value means.
    """
    wanted = (configured or "").strip()
    lowered = wanted.lower()
    if not wanted:
        return set()
    ids: set[str] = set()
    for label in lr.async_get(hass).labels.values():
        if (
            label.label_id == wanted
            or label.name.strip().lower() == lowered
            or label.label_id == slugify(lowered)
        ):
            ids.add(label.label_id)
    return ids


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
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    @callback
    def _forget_manager() -> None:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    entry.async_on_unload(_forget_manager)
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
        # Reference point for seeding staleness clocks: anything a member
        # state reported before this belongs to a previous HA run.
        self.started_at = dt_util.utcnow()

        options = entry.options
        self.device_classes: list[str] = list(
            options.get(CONF_DEVICE_CLASSES, DEFAULT_DEVICE_CLASSES)
        )
        self.stale_hours: float = _as_float(
            options.get(CONF_STALE_HOURS), DEFAULT_STALE_HOURS
        )
        self.battery_threshold: float = _as_float(
            options.get(CONF_BATTERY_THRESHOLD), DEFAULT_BATTERY_THRESHOLD
        )
        self.exclude_label: str = str(
            options.get(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL)
        )
        self.trend_minutes: float = min(
            TREND_MINUTES_MAX,
            max(
                TREND_MINUTES_MIN,
                _as_float(options.get(CONF_TREND_MINUTES), DEFAULT_TREND_MINUTES),
            ),
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
        self._prune_orphans()

        _LOGGER.info(
            "Area Averages started: device classes %s, exclude label %r -> %s, "
            "%d averages across %d areas (stale after %s h, trend window %s min)",
            self.device_classes,
            self.exclude_label,
            sorted(self._exclude_label_ids) or "no matching label",
            len(self._entities),
            len({area_id for area_id, _ in self._entities}),
            self.stale_hours,
            self.trend_minutes,
        )

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
        """Map the configured label (id, name or slug) to label registry ids."""
        configured = self.exclude_label.strip()
        ids = resolve_label_ids(self.hass, configured)
        if configured and not ids:
            # Silence here is dangerous: every exclusion vanishes and an
            # empty `excluded` list is indistinguishable from healthy.
            _LOGGER.warning(
                "Area Averages: exclude label %r matches no label in the label "
                "registry — NOTHING is being excluded from any average. "
                "Pick an existing label in the integration options.",
                configured,
            )
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

    @callback
    def _prune_orphans(self) -> None:
        """Drop registry entries left behind by a previous configuration.

        An options change reloads the entry, so the in-memory bookkeeping in
        _refresh() starts empty and cannot notice that (say) every humidity
        average is gone. Those entries would otherwise stay in the registry
        forever, owned by this entry and served as unavailable.

        Only genuine removals are deleted: an average that comes back later
        with the same unique_id reclaims its old entity_id and history.
        """
        if self.device_classes and not self._entities:
            # Something is wrong (registries not ready, every area lost its
            # members at once). Deleting registry entries here would throw
            # away real history; leaving orphans is the cheaper mistake.
            _LOGGER.warning(
                "Area Averages: %s configured but no averages resolved — "
                "skipping orphan cleanup rather than removing entities",
                self.device_classes,
            )
            return
        ent_reg = er.async_get(self.hass)
        desired = {
            calc.average_unique_id(area_id, device_class)
            for area_id, device_class in self._entities
        }
        existing = [
            (entry.entity_id, entry.unique_id)
            for entry in er.async_entries_for_config_entry(
                ent_reg, self.entry.entry_id
            )
            # Only our own average sensors; the config switch lives on the
            # same config entry and must never be pruned.
            if entry.domain == "sensor"
            and entry.platform == DOMAIN
            and str(entry.unique_id).endswith("_average")
        ]
        for entity_id in calc.orphan_unique_ids(desired, existing):
            _LOGGER.info(
                "Area Averages: removing orphaned entity %s (no longer produced "
                "by the current options)",
                entity_id,
            )
            ent_reg.async_remove(entity_id)

    async def _async_remove_entity(self, entity: AreaAverageSensor) -> None:
        """Remove an average whose area lost all members (or was deleted)."""
        entity_id = entity.entity_id
        await entity.async_remove(force_remove=True)
        ent_reg = er.async_get(self.hass)
        if entity_id and ent_reg.async_get(entity_id) is not None:
            ent_reg.async_remove(entity_id)

    # ------------------------------------------------------- diagnostics

    @callback
    def diagnostics(self) -> dict[str, Any]:
        """Snapshot for diagnostics.py (no I/O, safe on the event loop)."""
        return {
            "options": {
                CONF_DEVICE_CLASSES: self.device_classes,
                CONF_STALE_HOURS: self.stale_hours,
                CONF_BATTERY_THRESHOLD: self.battery_threshold,
                CONF_EXCLUDE_LABEL: self.exclude_label,
                CONF_TREND_MINUTES: self.trend_minutes,
            },
            "exclude_label_ids": sorted(self._exclude_label_ids),
            "started_at": self.started_at.isoformat(),
            "averages": [
                entity.diagnostics() for entity in self._entities.values()
            ],
        }


def _as_float(value: Any, default: float) -> float:
    """Options come back from the UI as str/int/float; never explode."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


class AreaAverageSensor(SensorEntity, RestoreEntity):
    """Mean of the reporting sensors of one device class in one area."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({ATTR_SENSORS, ATTR_EXCLUDED, ATTR_OFFLINE})

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
        self._trend_samples: deque[tuple[float, float]] = deque(
            maxlen=TREND_MAX_SAMPLES
        )
        self._warned_units: set[tuple[str, str]] = set()

        self._attr_unique_id = calc.average_unique_id(area_id, device_class)
        try:
            self._attr_device_class = SensorDeviceClass(device_class)
        except ValueError:
            self._attr_device_class = None
        if device_class in SUPPORTED_DEVICE_CLASSES:
            # Icons come from icons.json rather than the dashboard.
            self._attr_translation_key = device_class
        # Every supported device class has a fixed unit. A MEASUREMENT
        # sensor that starts unitless and later adopts a member's unit makes
        # the recorder throw away its long-term statistics.
        self._attr_native_unit_of_measurement = calc.fixed_unit(
            device_class,
            manager.hass.config.units.temperature_unit,
            manager.hass.config.units.pressure_unit,
        )
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

    @callback
    def _warn_unit(self, unit: str, target: str) -> None:
        """Warn once per (unit, target) pair — _recompute runs constantly."""
        key = (str(unit), str(target))
        if key in self._warned_units:
            return
        self._warned_units.add(key)
        _LOGGER.warning(
            "%s: dropping members reporting %s — no conversion into %s. "
            "Label them %r to exclude them explicitly.",
            self.entity_id or self._attr_unique_id,
            unit,
            target,
            self._manager.exclude_label,
        )

    def _converted(self, value: float, unit: str | None) -> float | None:
        """Convert a member value into this sensor's unit if needed."""
        target = self._attr_native_unit_of_measurement
        if target is None:
            # Only reachable for a device class with no fixed unit.
            self._attr_native_unit_of_measurement = unit
            return value
        if unit is None or calc.normalize_unit(unit) == calc.normalize_unit(target):
            return value
        converter = UNIT_CONVERTERS.get(self._device_class_key)
        if converter is not None:
            try:
                if unit in converter.VALID_UNITS and target in converter.VALID_UNITS:
                    return converter.convert(value, unit, target)
            except (TypeError, ValueError, KeyError, ZeroDivisionError):
                pass
        self._warn_unit(unit, target)
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
        self._seed_last_numeric()
        self._subscribe()
        ent_reg = er.async_get(self.hass)
        reg_entry = ent_reg.async_get(self.entity_id)
        if reg_entry is not None and reg_entry.area_id is None:
            ent_reg.async_update_entity(self.entity_id, area_id=self._area_id)
        self._recompute()

    @callback
    def _seed_last_numeric(self) -> None:
        """Seed staleness clocks for members with no persisted timestamp.

        Only from reports that predate this run — a report made after we
        started will be picked up by _recompute anyway. A member that has
        never produced a number keeps no timestamp at all and is reported
        as offline rather than stale, which is the honest answer.
        """
        reference = self._manager.started_at
        for entity_id in self._membership.members:
            if entity_id in self._last_numeric:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or self._numeric_value(state) is None:
                # No number now means no evidence it ever reported one; a
                # timestamp here would make diagnostics claim a numeric report
                # at an instant the member was unavailable.
                continue
            last = state.last_reported or state.last_updated
            if last is not None and last < reference:
                self._last_numeric[entity_id] = last

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
    def _sample_members(
        self, names: dict[str, str]
    ) -> list[calc.MemberSample]:
        """One snapshot per member, already converted into our unit."""
        samples: list[calc.MemberSample] = []
        for entity_id in self._membership.members:
            state = self.hass.states.get(entity_id)
            value: float | None = None
            if state is None:
                names[entity_id] = entity_id
            else:
                names[entity_id] = state.name or entity_id
                raw = self._numeric_value(state)
                if raw is not None:
                    # Time of the last actual report, even if the value
                    # repeated. Recorded before conversion: it is a fact
                    # about the member, not about our unit.
                    last = state.last_reported or state.last_updated
                    if last is not None:
                        self._last_numeric[entity_id] = last
                    value = self._converted(
                        raw, state.attributes.get("unit_of_measurement")
                    )
            stamp = self._last_numeric.get(entity_id)
            samples.append(
                calc.MemberSample(
                    entity_id,
                    value,
                    stamp.timestamp() if stamp is not None else None,
                )
            )
        return samples

    @callback
    def _battery_status(
        self,
    ) -> tuple[list[str], list[tuple[str, str, float | None]]]:
        """(low_battery entity_ids, issue rows) for the members' batteries."""
        low_battery: list[str] = []
        rows: list[tuple[str, str, float | None]] = []
        owner: dict[str, str] = {}
        for member, batteries in self._membership.batteries.items():
            for battery_id in batteries:
                owner.setdefault(battery_id, member)
        for battery_id in sorted(owner):
            state = self.hass.states.get(battery_id)
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                continue
            if battery_id.startswith("binary_sensor."):
                if state.state == "on":
                    low_battery.append(battery_id)
                    rows.append((state.name, battery_id, None))
                continue
            try:
                level = float(state.state)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(level):
                continue
            if level <= self._manager.battery_threshold:
                low_battery.append(battery_id)
                rows.append((state.name, battery_id, level))
        return low_battery, rows

    @callback
    def _track_trend(self, now: float, mean: float | None) -> tuple[str, float | None]:
        """Bounded in-memory trend. No recorder, no persistence, no I/O."""
        window = self._manager.trend_minutes * 60.0
        if mean is not None:
            # Keep the samples spread out, or a chatty room fills the deque
            # in two minutes and the trend can never span long enough.
            gap = max(TREND_MIN_SAMPLE_GAP, window / TREND_MAX_SAMPLES)
            if not self._trend_samples or now - self._trend_samples[-1][0] >= gap:
                self._trend_samples.append((now, mean))
        points = [
            (stamp, value)
            for stamp, value in self._trend_samples
            if now - stamp <= window
        ]
        return calc.trend_of(points)

    @callback
    def _recompute(self) -> None:
        if self.hass is None:
            return
        now = dt_util.utcnow()
        now_ts = now.timestamp()
        stale_seconds = max(self._manager.stale_hours, 0.0) * 3600.0

        names: dict[str, str] = {}
        samples = self._sample_members(names)
        reporting, stale, offline = calc.classify_members(
            samples, now_ts, stale_seconds
        )
        values = [value for _, value in reporting]

        stale_rows: list[tuple[str, str, int]] = []
        for entity_id in stale:
            stamp = self._last_numeric.get(entity_id)
            hours = (
                int((now_ts - stamp.timestamp()) // 3600)
                if stamp is not None
                else int(self._manager.stale_hours)
            )
            stale_rows.append((names.get(entity_id, entity_id), entity_id, hours))

        low_battery, battery_rows = self._battery_status()
        issues = calc.issue_rows(
            [(names.get(entity_id, entity_id), entity_id) for entity_id in offline],
            stale_rows,
            battery_rows,
        )

        mean = round(fmean(values), 2) if values else None
        minimum, maximum, spread = calc.stats(values)
        trend, trend_rate = self._track_trend(now_ts, mean)

        self._attr_native_value = mean
        self._attr_extra_state_attributes = {
            ATTR_ISSUE: "; ".join(issues) if issues else None,
            ATTR_ISSUES: issues,
            ATTR_HEALTHY: calc.healthy_state(len(reporting), issues),
            ATTR_SUMMARY: calc.summary_text(
                len(self._membership.members), len(reporting)
            ),
            ATTR_SENSORS: list(self._membership.members),
            ATTR_EXCLUDED: list(self._membership.excluded),
            ATTR_MEMBER_COUNT: len(self._membership.members),
            ATTR_REPORTING: len(reporting),
            ATTR_OFFLINE: offline,
            ATTR_STALE: stale,
            ATTR_LOW_BATTERY: low_battery,
            ATTR_MINIMUM: minimum,
            ATTR_MAXIMUM: maximum,
            ATTR_SPREAD: spread,
            ATTR_TREND: trend,
            ATTR_TREND_RATE: trend_rate,
        }
        self.async_write_ha_state()

    # -------------------------------------------------------- diagnostics

    @callback
    def diagnostics(self) -> dict[str, Any]:
        """Per-average detail for diagnostics.py."""
        now_ts = dt_util.utcnow().timestamp()
        stale_seconds = max(self._manager.stale_hours, 0.0) * 3600.0
        names: dict[str, str] = {}
        samples = self._sample_members(names)
        reporting, stale, offline = calc.classify_members(
            samples, now_ts, stale_seconds
        )
        reporting_ids = {entity_id for entity_id, _ in reporting}
        members = []
        for sample in samples:
            if sample.entity_id in reporting_ids:
                classification = "reporting"
            elif sample.entity_id in stale:
                classification = "stale"
            else:
                classification = "offline"
            stamp = self._last_numeric.get(sample.entity_id)
            members.append(
                {
                    "entity_id": sample.entity_id,
                    "name": names.get(sample.entity_id),
                    "classification": classification,
                    "value_in_average_unit": sample.value,
                    "last_numeric": stamp.isoformat() if stamp is not None else None,
                }
            )
        return {
            "entity_id": self.entity_id,
            "unique_id": self._attr_unique_id,
            "area_id": self._area_id,
            "device_class": self._device_class_key,
            "unit": self._attr_native_unit_of_measurement,
            "state": self._attr_native_value,
            "members": members,
            "excluded": list(self._membership.excluded),
            "batteries": {
                member: list(batteries)
                for member, batteries in self._membership.batteries.items()
            },
            "trend_samples": len(self._trend_samples),
            "unconvertible_units": sorted(
                {unit for unit, _ in self._warned_units}
            ),
        }
