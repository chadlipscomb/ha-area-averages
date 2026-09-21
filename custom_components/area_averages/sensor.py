"""Sensor platform for Area Averages.

One average sensor is created per CONFIG SUBENTRY -- one row on the
integration page, one sensor, one area, one device class. Which entities go
into it is decided by the subentry's mode:

  exclude    every sensor of that class in the area, minus anything
             carrying the exclude label. The original behaviour.
  include    only the entities the user picked, plus anything carrying the
             subentry's include label.

Membership is then resolved from the entity registry, so a sensor moved
into the area is picked up automatically in exclude mode without a restart.

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
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PressureConverter, TemperatureConverter

from . import calc, registry
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
    DATA_AUTO_CREATED,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_BATTERY_THRESHOLD,
    DEFAULT_DEVICE_CLASSES,
    DEFAULT_EXCLUDE_LABEL,
    DEFAULT_STALE_HOURS,
    DEFAULT_TREND_MINUTES,
    DOMAIN,
    RELOAD_OPTION_KEYS,
    SOURCE_AUTO,
    SUBENTRY_TYPE,
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


@dataclass
class AverageConfig:
    """What one subentry says an average IS.

    ``area_id`` and ``device_class`` are the average's identity -- they set
    the unique_id, so the reconfigure flow refuses to change them.
    """

    subentry_id: str
    area_id: str
    device_class: str
    mode: str = calc.MODE_EXCLUDE
    include_entities: tuple[str, ...] = ()
    include_label: str | None = None
    source: str = SOURCE_AUTO

    @classmethod
    def from_subentry(cls, subentry: ConfigSubentry) -> AverageConfig | None:
        """Read a subentry, or None when its data is unusable."""
        data = subentry.data
        area_id = str(data.get(CONF_AREA_ID) or "")
        device_class = str(data.get(CONF_DEVICE_CLASS) or "")
        if not area_id or not device_class:
            _LOGGER.warning(
                "Area Averages: ignoring subentry %s (%s) -- it has no area or "
                "no device class",
                subentry.subentry_id,
                subentry.title,
            )
            return None
        mode = str(data.get(CONF_MODE) or calc.MODE_EXCLUDE)
        if mode not in calc.MODES:
            mode = calc.MODE_EXCLUDE
        return cls(
            subentry_id=subentry.subentry_id,
            area_id=area_id,
            device_class=device_class,
            mode=mode,
            include_entities=tuple(
                str(entity_id) for entity_id in data.get(CONF_INCLUDE_ENTITIES) or ()
            ),
            include_label=data.get(CONF_INCLUDE_LABEL) or None,
            source=str(data.get(CONF_SOURCE) or SOURCE_AUTO),
        )

    @property
    def key(self) -> str:
        return calc.subentry_key(self.area_id, self.device_class)


@dataclass
class Membership:
    """Resolved membership of one average."""

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
    async_add_entities: AddConfigEntryEntitiesCallback,
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
    """Keeps one sensor per subentry, and their membership, up to date."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._async_add_entities = async_add_entities
        #: One sensor per subentry, keyed by subentry_id.
        self._entities: dict[str, AreaAverageSensor] = {}
        self._pending_refresh: CALLBACK_TYPE | None = None
        self._exclude_label_ids: frozenset[str] = frozenset()
        self._label_cache: dict[str, frozenset[str]] = {}
        # Reference point for seeding staleness clocks: anything a member
        # state reported before this belongs to a previous HA run.
        self.started_at = dt_util.utcnow()

        options = entry.options
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
        self.auto_discover: bool = bool(
            options.get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)
        )
        self.auto_discover_classes: list[str] = [
            device_class
            for device_class in options.get(
                CONF_AUTO_DISCOVER_CLASSES, DEFAULT_DEVICE_CLASSES
            )
            if device_class in SUPPORTED_DEVICE_CLASSES
        ]

    async def async_start(self) -> None:
        """Build the sensors, subscribe to changes, then discover."""
        self._resolve_labels()

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

        self._reconcile()
        self._prune_orphans()
        self._run_discovery()

        _LOGGER.info(
            "Area Averages started: %d averages across %d areas, exclude label "
            "%r -> %s, auto-discover %s%s (stale after %s h, trend window %s min)",
            len(self._entities),
            len({entity.config.area_id for entity in self._entities.values()}),
            self.exclude_label,
            sorted(self._exclude_label_ids) or "no matching label",
            "on" if self.auto_discover else "off",
            f" for {self.auto_discover_classes}" if self.auto_discover else "",
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
            self._resolve_labels()
        self._schedule_refresh()

    @callback
    def options_need_reload(self, entry: ConfigEntry) -> bool:
        """True when a setting this manager CACHED has changed.

        Adding, editing or deleting one average must not tear down every
        other average in the house -- that would throw away their in-memory
        trend history for no reason.
        """
        current = {
            CONF_STALE_HOURS: self.stale_hours,
            CONF_BATTERY_THRESHOLD: self.battery_threshold,
            CONF_EXCLUDE_LABEL: self.exclude_label,
            CONF_TREND_MINUTES: self.trend_minutes,
            CONF_AUTO_DISCOVER: self.auto_discover,
            CONF_AUTO_DISCOVER_CLASSES: self.auto_discover_classes,
        }
        options = entry.options
        for key in RELOAD_OPTION_KEYS:
            if key not in options:
                continue
            stored: Any = options[key]
            if key in (CONF_STALE_HOURS, CONF_BATTERY_THRESHOLD, CONF_TREND_MINUTES):
                stored = _as_float(stored, float(current[key]))
                if key == CONF_TREND_MINUTES:
                    stored = min(TREND_MINUTES_MAX, max(TREND_MINUTES_MIN, stored))
            elif key == CONF_AUTO_DISCOVER:
                stored = bool(stored)
            elif key == CONF_AUTO_DISCOVER_CLASSES:
                stored = [
                    device_class
                    for device_class in stored
                    if device_class in SUPPORTED_DEVICE_CLASSES
                ]
            elif key == CONF_EXCLUDE_LABEL:
                stored = str(stored)
            if stored != current[key]:
                return True
        return False

    @callback
    def handle_entry_update(self) -> None:
        """A subentry was added, edited or deleted. Sync without a reload.

        Immediately, so a newly added average shows up straight away; the
        debounced pass afterwards is what runs discovery, and collapses the
        burst of updates discovery itself causes.
        """
        self._reconcile()
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
        self._reconcile()
        self._run_discovery()

    @callback
    def _handle_periodic(self, _arg: Any = None) -> None:
        for entity in self._entities.values():
            entity.async_refresh_state()

    # --------------------------------------------------------- membership

    @callback
    def _resolve_labels(self) -> None:
        """Map the configured labels (id, name or slug) to registry ids."""
        self._label_cache = {}
        configured = self.exclude_label.strip()
        ids = self._label_ids(configured)
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
    def _label_ids(self, configured: str | None) -> frozenset[str]:
        """Resolved label ids for a configured label, memoised per refresh."""
        text = (configured or "").strip()
        if not text:
            return frozenset()
        if text not in self._label_cache:
            self._label_cache[text] = frozenset(
                registry.resolve_label_ids(self.hass, text)
            )
        return self._label_cache[text]

    @callback
    def _configs(self) -> dict[str, AverageConfig]:
        """Every usable average subentry, keyed by subentry_id."""
        configs: dict[str, AverageConfig] = {}
        for subentry in self.entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_TYPE:
                continue
            config = AverageConfig.from_subentry(subentry)
            if config is not None:
                configs[subentry.subentry_id] = config
        return configs

    @callback
    def _reconcile(self) -> None:
        """One pass: one sensor per subentry, each with fresh membership.

        Membership is resolved BEFORE a new sensor is handed to Home
        Assistant, so the entity never has to be written twice -- once
        empty, once correct -- and never publishes a momentary
        "no sensors reporting".
        """
        configs = self._configs()

        for subentry_id in [k for k in self._entities if k not in configs]:
            entity = self._entities.pop(subentry_id)
            self.hass.async_create_task(self._async_remove_entity(entity))

        if not configs:
            return

        index = registry.candidate_index(
            self.hass, {config.device_class for config in configs.values()}
        )

        for subentry_id, config in configs.items():
            candidates = index.get((config.area_id, config.device_class), [])
            members, excluded = calc.select_members(
                candidates,
                config.mode,
                include_entities=config.include_entities,
                include_label_ids=self._label_ids(config.include_label),
                exclude_label_ids=self._exclude_label_ids,
            )
            membership = Membership(
                members=members,
                batteries=registry.battery_entities(self.hass, members),
                excluded=excluded,
            )

            entity = self._entities.get(subentry_id)
            if entity is not None:
                entity.async_set_config(config)
                entity.async_set_membership(membership)
                continue

            entity = AreaAverageSensor(self, config)
            entity.async_set_membership(membership)
            self._entities[subentry_id] = entity
            # One call per subentry: this is what puts the entity on that
            # row of the integration page.
            self._async_add_entities([entity], config_subentry_id=subentry_id)

    @callback
    def _prune_orphans(self) -> None:
        """Drop registry entries left behind by a previous configuration.

        Deleting a subentry already removes its entity (core clears the
        registry for us), so this only catches leftovers from before the
        move to subentries, or from a migration that could not match a
        unique_id.

        An average that comes back later with the same unique_id reclaims
        its old entity_id and history, so only genuine strangers go.
        """
        if self.entry.subentries and not self._entities:
            # Something is wrong (registries not ready, every subentry
            # unreadable). Deleting registry entries here would throw away
            # real history; leaving orphans is the cheaper mistake.
            _LOGGER.warning(
                "Area Averages: %d subentries configured but no averages "
                "resolved — skipping orphan cleanup rather than removing "
                "entities",
                len(self.entry.subentries),
            )
            return
        ent_reg = er.async_get(self.hass)
        desired = {
            calc.average_unique_id(entity.config.area_id, entity.config.device_class)
            for entity in self._entities.values()
        }
        existing = [
            (reg_entry.entity_id, reg_entry.unique_id)
            for reg_entry in er.async_entries_for_config_entry(
                ent_reg, self.entry.entry_id
            )
            # Only our own average sensors; the config switches live on the
            # same config entry and must never be pruned.
            if reg_entry.domain == "sensor"
            and reg_entry.platform == DOMAIN
            and str(reg_entry.unique_id).endswith("_average")
        ]
        for entity_id in calc.orphan_unique_ids(desired, existing):
            _LOGGER.info(
                "Area Averages: removing orphaned entity %s (no longer produced "
                "by the current configuration)",
                entity_id,
            )
            ent_reg.async_remove(entity_id)

    async def _async_remove_entity(self, entity: AreaAverageSensor) -> None:
        """Remove the sensor of a subentry the user deleted.

        Core removes the registry entry when the subentry goes, and the
        entity then removes itself; async_remove is idempotent, so this is
        belt and braces for an entity that never had a registry entry.
        """
        try:
            await entity.async_remove(force_remove=True)
        except Exception:  # noqa: BLE001 - never let cleanup break a reload
            _LOGGER.debug(
                "Area Averages: %s was already gone", entity.entity_id, exc_info=True
            )

    # ------------------------------------------------------- discovery

    @callback
    def _run_discovery(self) -> None:
        """Materialise a subentry for every average worth having.

        Deliberately visible: a discovered average is a real row that can
        be edited or deleted like any other. Deleting one records it as
        ignored, so discovery does not simply put it back.
        """
        if not self.auto_discover:
            return

        configs = self._configs()
        existing_keys = {config.key for config in configs.values()}
        self._record_deletions(existing_keys)

        ignored = {str(key) for key in self.entry.options.get(CONF_IGNORED, [])}
        skip = ignored | existing_keys
        area_reg = ar.async_get(self.hass)
        index = registry.candidate_index(self.hass, set(self.auto_discover_classes))

        created: list[str] = []
        for (area_id, device_class), candidates in sorted(index.items()):
            key = calc.subentry_key(area_id, device_class)
            if key in skip or area_reg.async_get_area(area_id) is None:
                continue
            members, _ = calc.select_members(
                candidates,
                calc.MODE_EXCLUDE,
                exclude_label_ids=self._exclude_label_ids,
            )
            if not members:
                continue
            subentry = ConfigSubentry(
                data={
                    CONF_AREA_ID: area_id,
                    CONF_DEVICE_CLASS: device_class,
                    CONF_MODE: calc.MODE_EXCLUDE,
                    CONF_INCLUDE_ENTITIES: [],
                    CONF_INCLUDE_LABEL: None,
                    CONF_SOURCE: SOURCE_AUTO,
                },
                subentry_type=SUBENTRY_TYPE,
                title=calc.average_title(
                    area_reg.async_get_area(area_id).name, device_class
                ),
                unique_id=key,
            )
            try:
                self.hass.config_entries.async_add_subentry(self.entry, subentry)
            except AbortFlow:
                # Raced with someone adding the same average by hand.
                continue
            created.append(key)

        if not created:
            return
        _LOGGER.info(
            "Area Averages: auto-discovered %d average(s): %s",
            len(created),
            ", ".join(created),
        )
        self._remember_auto_created(created)
        # Build the sensors here rather than waiting for the update listener:
        # the first discovery happens during setup, before that listener is
        # registered, and its averages must not wait for the next event.
        self._reconcile()

    @callback
    def _record_deletions(self, existing_keys: set[str]) -> None:
        """A discovered average the user deleted must stay deleted."""
        auto_created = [str(key) for key in self.entry.data.get(DATA_AUTO_CREATED, [])]
        deleted = [key for key in auto_created if key not in existing_keys]
        if not deleted:
            return
        ignored = {str(key) for key in self.entry.options.get(CONF_IGNORED, [])}
        _LOGGER.info(
            "Area Averages: %s was deleted and will not be auto-discovered again "
            "until it is un-ignored in the options",
            ", ".join(sorted(deleted)),
        )
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                DATA_AUTO_CREATED: [
                    key for key in auto_created if key in existing_keys
                ],
            },
            options={
                **self.entry.options,
                CONF_IGNORED: sorted(ignored | set(deleted)),
            },
        )

    @callback
    def _remember_auto_created(self, created: list[str]) -> None:
        """Record what discovery made, so a later deletion can be noticed."""
        auto_created = {str(key) for key in self.entry.data.get(DATA_AUTO_CREATED, [])}
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                DATA_AUTO_CREATED: sorted(auto_created | set(created)),
            },
        )

    # ------------------------------------------------------- diagnostics

    @callback
    def diagnostics(self) -> dict[str, Any]:
        """Snapshot for diagnostics.py (no I/O, safe on the event loop)."""
        return {
            "options": {
                CONF_STALE_HOURS: self.stale_hours,
                CONF_BATTERY_THRESHOLD: self.battery_threshold,
                CONF_EXCLUDE_LABEL: self.exclude_label,
                CONF_TREND_MINUTES: self.trend_minutes,
                CONF_AUTO_DISCOVER: self.auto_discover,
                CONF_AUTO_DISCOVER_CLASSES: self.auto_discover_classes,
                CONF_IGNORED: sorted(
                    str(key) for key in self.entry.options.get(CONF_IGNORED, [])
                ),
            },
            "auto_created": sorted(
                str(key) for key in self.entry.data.get(DATA_AUTO_CREATED, [])
            ),
            "exclude_label_ids": sorted(self._exclude_label_ids),
            "started_at": self.started_at.isoformat(),
            "subentries": [
                {
                    "subentry_id": subentry.subentry_id,
                    "title": subentry.title,
                    "unique_id": subentry.unique_id,
                    "data": dict(subentry.data),
                }
                for subentry in self.entry.subentries.values()
                if subentry.subentry_type == SUBENTRY_TYPE
            ],
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
    """Mean of the reporting members of one average."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({ATTR_SENSORS, ATTR_EXCLUDED, ATTR_OFFLINE})

    def __init__(
        self,
        manager: AreaAveragesManager,
        config: AverageConfig,
    ) -> None:
        self._manager = manager
        self.config = config
        self._area_id = config.area_id
        self._device_class_key = config.device_class
        self._membership = Membership()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._last_numeric: dict[str, Any] = {}
        self._trend_samples: deque[tuple[float, float]] = deque(
            maxlen=TREND_MAX_SAMPLES
        )
        self._warned_units: set[tuple[str, str]] = set()

        self._attr_unique_id = calc.average_unique_id(
            config.area_id, config.device_class
        )
        try:
            self._attr_device_class = SensorDeviceClass(config.device_class)
        except ValueError:
            self._attr_device_class = None
        if config.device_class in SUPPORTED_DEVICE_CLASSES:
            # Icons come from icons.json rather than the dashboard.
            self._attr_translation_key = config.device_class
        # Every supported device class has a fixed unit. A MEASUREMENT
        # sensor that starts unitless and later adopts a member's unit makes
        # the recorder throw away its long-term statistics.
        self._attr_native_unit_of_measurement = calc.fixed_unit(
            config.device_class,
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
        self._attr_name = calc.average_title(area_name, self._device_class_key)

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
    def async_set_config(self, config: AverageConfig) -> None:
        """Called by the manager when the subentry was edited."""
        self.config = config
        self._refresh_name()

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
            "subentry_id": self.config.subentry_id,
            "area_id": self._area_id,
            "device_class": self._device_class_key,
            "mode": self.config.mode,
            "include_entities": list(self.config.include_entities),
            "include_label": self.config.include_label,
            "source": self.config.source,
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
