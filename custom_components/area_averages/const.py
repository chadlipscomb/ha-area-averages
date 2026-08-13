"""Constants for the Area Averages integration."""

DOMAIN = "area_averages"

CONF_DEVICE_CLASSES = "device_classes"
CONF_STALE_HOURS = "stale_hours"
CONF_BATTERY_THRESHOLD = "battery_threshold"
CONF_EXCLUDE_LABEL = "exclude_label"
CONF_TREND_MINUTES = "trend_minutes"

DEFAULT_DEVICE_CLASSES = ["temperature"]
DEFAULT_STALE_HOURS = 24
DEFAULT_BATTERY_THRESHOLD = 20
DEFAULT_EXCLUDE_LABEL = "exclude-average"
DEFAULT_TREND_MINUTES = 120

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
