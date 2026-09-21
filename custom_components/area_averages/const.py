"""Constants for the Area Averages integration."""

DOMAIN = "area_averages"

#: Config entry version. 1 = global device-class list, averages implied.
#: 2 = one subentry per average (see __init__.async_migrate_entry).
CONFIG_VERSION = 2

#: The one subentry type this integration offers: a single average.
SUBENTRY_TYPE = "average"

# ---------------------------------------------------------- global options

CONF_STALE_HOURS = "stale_hours"
CONF_BATTERY_THRESHOLD = "battery_threshold"
CONF_EXCLUDE_LABEL = "exclude_label"
CONF_TREND_MINUTES = "trend_minutes"
CONF_AUTO_DISCOVER = "auto_discover"
CONF_AUTO_DISCOVER_CLASSES = "auto_discover_classes"
#: Keys ("<area_id>|<device_class>") auto-discovery must not re-create,
#: because the user deleted that average by hand.
CONF_IGNORED = "ignored"

#: Removed in version 2, read once by the migration.
CONF_DEVICE_CLASSES = "device_classes"

# ------------------------------------------------------------ subentry data

CONF_AREA_ID = "area_id"
CONF_DEVICE_CLASS = "device_class"
CONF_MODE = "mode"
CONF_INCLUDE_ENTITIES = "include_entities"
CONF_INCLUDE_LABEL = "include_label"
CONF_SOURCE = "source"

SOURCE_USER = "user"
SOURCE_AUTO = "auto"

# ------------------------------------------------------------- entry data

#: Bookkeeping, never shown: keys auto-discovery has created. A key in here
#: with no matching subentry means the user deleted it.
DATA_AUTO_CREATED = "auto_created"

# ------------------------------------------------------------- defaults

DEFAULT_DEVICE_CLASSES = ["temperature"]
DEFAULT_STALE_HOURS = 24
DEFAULT_BATTERY_THRESHOLD = 20
DEFAULT_EXCLUDE_LABEL = "exclude-average"
DEFAULT_TREND_MINUTES = 120
#: Off on a fresh install: averages are explicit now. The migration turns it
#: on, because that is what a version 1 entry was doing.
DEFAULT_AUTO_DISCOVER = False

#: Changing one of these rebuilds every average, because the manager caches
#: them. Everything else in options is bookkeeping and is re-read live.
RELOAD_OPTION_KEYS = frozenset(
    {
        CONF_STALE_HOURS,
        CONF_BATTERY_THRESHOLD,
        CONF_EXCLUDE_LABEL,
        CONF_TREND_MINUTES,
        CONF_AUTO_DISCOVER,
        CONF_AUTO_DISCOVER_CLASSES,
    }
)

# Device classes the integration knows how to average.
# Add new ones here (and a unit rule in calc.STATIC_UNITS / calc.fixed_unit).
SUPPORTED_DEVICE_CLASSES = [
    "temperature",
    "humidity",
    "pressure",
    "carbon_dioxide",
    "illuminance",
    "pm25",
]

ATTR_ISSUE = "issue"
ATTR_ISSUES = "issues"
ATTR_SENSORS = "sensors"
ATTR_REPORTING = "reporting"
ATTR_MEMBER_COUNT = "member_count"
ATTR_EXCLUDED = "excluded"
ATTR_STALE = "stale"
ATTR_LOW_BATTERY = "low_battery"

# Added in 0.2.0 — see README and the frontend contract.
ATTR_OFFLINE = "offline"
ATTR_SUMMARY = "summary"
ATTR_HEALTHY = "healthy"
ATTR_MINIMUM = "minimum"
ATTR_MAXIMUM = "maximum"
ATTR_SPREAD = "spread"
ATTR_TREND = "trend"
ATTR_TREND_RATE = "trend_rate"
