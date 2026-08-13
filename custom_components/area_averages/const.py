"""Constants for the Area Averages integration."""

DOMAIN = "area_averages"

CONF_DEVICE_CLASSES = "device_classes"
CONF_STALE_HOURS = "stale_hours"
CONF_BATTERY_THRESHOLD = "battery_threshold"
CONF_EXCLUDE_LABEL = "exclude_label"

DEFAULT_DEVICE_CLASSES = ["temperature"]
DEFAULT_STALE_HOURS = 24
DEFAULT_BATTERY_THRESHOLD = 20
DEFAULT_EXCLUDE_LABEL = "exclude-average"

# Device classes the integration knows how to average.
# Add new ones here (and a converter/unit rule in sensor.py if needed).
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
