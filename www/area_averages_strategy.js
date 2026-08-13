/* Area Averages dashboard strategy.
 *
 * Generates the whole "Area Temperatures" dashboard at load time from the
 * live area_averages sensors, so sections track reality: new sensors, new
 * areas, exclusions — no regeneration step. Members that are currently
 * unavailable/unknown hide themselves (native visibility conditions) and an
 * "Offline sensors" toggle tile appears in any room that is hiding some;
 * it flips input_boolean.show_offline_sensors to reveal them.
 *
 * Registered as a lovelace resource (/local/area_averages_strategy.js).
 * Dashboard config is just: {"strategy": {"type": "custom:area-temperatures"}}
 * Optional strategy options: history_hours (default 24).
 *
 * NOTE: do not "take control" of the dashboard in the UI editor — that
 * freezes the current snapshot into a static config.
 */

const VERSION = "1.0.0";
const PLATFORM = "area_averages";
const TOGGLE = "input_boolean.show_offline_sensors";
const OFFLINE = ["unavailable", "unknown"];

const CLASS_META = {
  temperature: { title: "Rooms", path: "rooms", icon: "mdi:thermometer" },
  humidity: { title: "Humidity", path: "humidity", icon: "mdi:water-percent" },
  pressure: { title: "Pressure", path: "pressure", icon: "mdi:gauge" },
  carbon_dioxide: { title: "CO2", path: "co2", icon: "mdi:molecule-co2" },
  illuminance: { title: "Light", path: "light", icon: "mdi:brightness-5" },
  pm25: { title: "PM2.5", path: "pm25", icon: "mdi:blur" },
};

const STATUS_MARKDOWN = `
{% set ns = namespace(rows=[]) %}
{% for e in integration_entities('area_averages') | sort %}
{% for i in state_attr(e, 'issues') or [] %}
{% set ns.rows = ns.rows + [(area_name(e) or e) ~ ' — ' ~ i] %}
{% endfor %}
{% endfor %}
{% if ns.rows %}
#### ⚠️ {{ ns.rows | count }} sensor issue{{ 's' if ns.rows | count > 1 else '' }}
{% for r in ns.rows %}
- {{ r }}
{% endfor %}
{% else %}
#### ✅ All sensors healthy
Every sensor has reported recently and no batteries are low.
{% endif %}
`;

function collectRooms(hass, deviceClass) {
  const rooms = [];
  for (const state of Object.values(hass.states)) {
    const reg = hass.entities ? hass.entities[state.entity_id] : undefined;
    if (!reg || reg.platform !== PLATFORM) continue;
    if ((state.attributes.device_class || "") !== deviceClass) continue;
    const areaId =
      reg.area_id ||
      (reg.device_id && hass.devices && hass.devices[reg.device_id]
        ? hass.devices[reg.device_id].area_id
        : undefined);
    const area = areaId && hass.areas ? hass.areas[areaId] : undefined;
    rooms.push({
      avg: state.entity_id,
      area:
        (area && area.name) ||
        (state.attributes.friendly_name || state.entity_id).replace(
          / Average .*$/,
          ""
        ),
      icon: area ? area.icon : undefined,
      members: state.attributes.sensors || [],
      excluded: state.attributes.excluded || [],
    });
  }
  rooms.sort((a, b) => a.area.localeCompare(b.area));
  return rooms;
}

/* Visible when the entity reports, or when the reveal toggle is on. */
function memberVisibility(entityId) {
  return [
    {
      condition: "or",
      conditions: [
        { condition: "state", entity: entityId, state_not: OFFLINE },
        { condition: "state", entity: TOGGLE, state: "on" },
      ],
    },
  ];
}

function memberTile(entityId, excluded) {
  const tile = {
    type: "tile",
    entity: entityId,
    state_content: ["state", "last_changed"],
    visibility: memberVisibility(entityId),
  };
  if (excluded) {
    tile.icon = "mdi:sigma-lower";
    tile.color = "disabled";
  }
  return tile;
}

/* The reveal toggle, shown only while this room hides offline members. */
function offlineToggleTile(room) {
  const anyOffline = [...room.members, ...room.excluded].map((m) => ({
    condition: "state",
    entity: m,
    state: OFFLINE,
  }));
  return {
    type: "tile",
    entity: TOGGLE,
    name: "Offline sensors",
    icon: "mdi:lan-disconnect",
    color: "grey",
    hide_state: true,
    tap_action: { action: "toggle" },
    visibility: [{ condition: "or", conditions: anyOffline }],
  };
}

function roomSection(room) {
  const cards = [
    {
      type: "heading",
      heading: room.area,
      heading_style: "title",
      ...(room.icon ? { icon: room.icon } : {}),
    },
    {
      type: "tile",
      entity: room.avg,
      name: "Room average",
      icon: "mdi:thermometer-auto",
      color: "deep-orange",
      state_content: ["state", "issue"],
      grid_options: { columns: "full" },
    },
  ];
  for (const member of room.members) cards.push(memberTile(member, false));
  for (const excluded of room.excluded) cards.push(memberTile(excluded, true));
  if (room.members.length + room.excluded.length > 0) {
    cards.push(offlineToggleTile(room));
  }
  return { type: "grid", cards };
}

function statusSection() {
  return {
    type: "grid",
    cards: [
      { type: "heading", heading: "Status", heading_style: "title" },
      {
        type: "markdown",
        content: STATUS_MARKDOWN,
        grid_options: { columns: "full" },
      },
    ],
  };
}

function roomsView(deviceClass, rooms) {
  const meta = CLASS_META[deviceClass] || {
    title: deviceClass,
    path: deviceClass.replace(/[^a-z0-9]+/g, "-"),
    icon: "mdi:chart-box-outline",
  };
  const sections = rooms.map(roomSection);
  if (deviceClass === "temperature") sections.unshift(statusSection());
  return {
    title: meta.title,
    path: meta.path,
    icon: meta.icon,
    type: "sections",
    max_columns: 3,
    sections,
  };
}

function historyView(rooms, hours) {
  return {
    title: "History",
    path: "history",
    icon: "mdi:chart-line",
    type: "sections",
    max_columns: 2,
    sections: rooms.map((room) => ({
      type: "grid",
      cards: [
        { type: "heading", heading: room.area, heading_style: "title" },
        {
          type: "history-graph",
          hours_to_show: hours,
          entities: [{ entity: room.avg }].concat(
            room.members.map((m) => ({ entity: m }))
          ),
          grid_options: { columns: "full" },
        },
      ],
    })),
  };
}

class AreaTemperaturesStrategy {
  static async generate(config, hass) {
    const hours = (config && config.history_hours) || 24;
    const classes = new Set();
    for (const state of Object.values(hass.states)) {
      const reg = hass.entities ? hass.entities[state.entity_id] : undefined;
      if (reg && reg.platform === PLATFORM && state.attributes.device_class) {
        classes.add(state.attributes.device_class);
      }
    }
    const ordered = [
      "temperature",
      ...[...classes].filter((c) => c !== "temperature").sort(),
    ];
    const views = [];
    for (const deviceClass of ordered) {
      if (!classes.has(deviceClass)) continue;
      const rooms = collectRooms(hass, deviceClass);
      if (!rooms.length) continue;
      views.push(roomsView(deviceClass, rooms));
      if (deviceClass === "temperature") views.push(historyView(rooms, hours));
    }
    if (!views.length) {
      views.push({
        title: "Rooms",
        type: "sections",
        sections: [statusSection()],
      });
    }
    return { views };
  }
}

customElements.define(
  "ll-strategy-dashboard-area-temperatures",
  AreaTemperaturesStrategy
);
console.info(`%c AREA-AVERAGES STRATEGY ${VERSION}`, "color: deeppink");
