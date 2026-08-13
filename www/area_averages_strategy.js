/* Area Averages dashboard strategy.
 *
 * Generates the whole "Area Temperatures" dashboard at load time from the
 * live area_averages sensors, so the page tracks reality: new sensors, new
 * areas, exclusions — no regeneration step.
 *
 * Shape: a Rooms view of one hero card per room, grouped Inside / Outdoors /
 * Equipment & Cold, above a Status card that states what is broken; one
 * subview per room (path room-<area_id>) carrying the member sensors, the
 * excluded ones and their history; and a Trends view built on core
 * statistics-graph cards.
 *
 * Colour never comes from JS: generate() runs once per page load, so a colour
 * computed here would freeze until reload. The mushroom path emits a Jinja
 * ladder, the native path emits one tile per band with numeric_state
 * visibility. Both come from the single BANDS table below.
 *
 * Custom cards (mushroom-template-card, mini-graph-card, stack-in-card) are
 * probed at runtime; if they are not defined the whole dashboard falls back to
 * core cards, still correctly coloured.
 *
 * Registered as a lovelace resource (/local/area_averages_strategy.js).
 * Dashboard config is just: {"strategy": {"type": "custom:area-temperatures"}}
 * Options: history_hours (24), stats_days (7), cards ("auto"|"mushroom"|
 * "native"), probe_timeout_ms (1500), dashboard_path, areas
 * ({"<area_id>": {"group": "inside|outdoors|equipment",
 *                 "band": "default|fridge|none"}}).
 *
 * NOTE: do not "take control" of the dashboard in the UI editor — that
 * freezes the current snapshot into a static config.
 */

const VERSION = "2.0.0";
const PLATFORM = "area_averages";
const OFFLINE = ["unavailable", "unknown"];
const MAX_OFFLINE_BADGES = 3;
const TOGGLES = [
  "switch.area_averages_show_offline",
  "input_boolean.show_offline_sensors",
];
const PROBE = ["mushroom-template-card", "mini-graph-card", "stack-in-card"];
const PROBE_TIMEOUT = 1500;
const DEFAULT_DASHBOARD = "room-temperatures";
/* Half of the 2-decimal resolution the averages publish. */
const EPS = 0.005;

const CLASS_META = {
  temperature: { title: "Rooms", path: "rooms", icon: "mdi:thermometer" },
  humidity: { title: "Humidity", path: "humidity", icon: "mdi:water-percent" },
  pressure: { title: "Pressure", path: "pressure", icon: "mdi:gauge" },
  carbon_dioxide: { title: "CO2", path: "co2", icon: "mdi:molecule-co2" },
  illuminance: { title: "Light", path: "light", icon: "mdi:brightness-5" },
  pm25: { title: "PM2.5", path: "pm25", icon: "mdi:blur" },
};

/* One table, two renderings. min is inclusive, max exclusive unless
 * maxInclusive. hex feeds mini-graph, color feeds mushroom and tile. */
const BANDS = {
  default: {
    bands: [
      { color: "blue", hex: "#2196f3", max: 65 },
      { color: "light-blue", hex: "#03a9f4", min: 65, max: 68 },
      { color: "green", hex: "#4caf50", min: 68, max: 78 },
      { color: "amber", hex: "#ffc107", min: 78, max: 84, maxInclusive: true },
      { color: "red", hex: "#f44336", min: 84 },
    ],
    hotAbove: 84,
    hotIcon: "mdi:fire",
  },
  fridge: {
    bands: [
      { color: "light-blue", hex: "#03a9f4", max: 45 },
      { color: "amber", hex: "#ffc107", min: 45, max: 50, maxInclusive: true },
      { color: "red", hex: "#f44336", min: 50 },
    ],
    hotAbove: 50,
    hotIcon: "mdi:alert-circle",
  },
  none: { bands: [], hotAbove: null, hotIcon: null },
};

const GROUPS = [
  { id: "inside", title: "Inside", icon: "mdi:home-variant-outline" },
  { id: "outdoors", title: "Outdoors", icon: "mdi:sun-thermometer-outline" },
  { id: "equipment", title: "Equipment & Cold", icon: "mdi:tools" },
];
const GROUP_IDS = GROUPS.map((g) => g.id);

/* Areas that are not rooms: no comfort semantics, own group. */
const AREA_DEFAULTS = {
  fridge: { group: "equipment", band: "fridge" },
  "3d_printer": { group: "equipment", band: "none" },
  bench: { group: "equipment", band: "none" },
  outside: { group: "outdoors", band: "none" },
  weather: { group: "outdoors", band: "none" },
};

/* Last resort only — the area registry icon wins whenever it exists. */
const ICON_FALLBACK = {
  bedroom: "mdi:bed", kitchen: "mdi:chef-hat", living_room: "mdi:sofa",
  office: "mdi:desk", fridge: "mdi:fridge", bench: "mdi:bench",
  kittens: "mdi:cat", outside: "mdi:sun-thermometer-outline",
  weather: "mdi:weather-cloudy", "3d_printer": "mdi:printer-3d-nozzle",
};

/* ---------------------------------------------------------------- options */

function readOptions(config) {
  const raw = config && typeof config === "object" ? config : {};
  const cards =
    raw.cards === "native" || raw.cards === "mushroom" ? raw.cards : "auto";
  const timeout = Number(raw.probe_timeout_ms);
  const heroColumns = Number(raw.hero_columns);
  return {
    hours: Number(raw.history_hours) > 0 ? Number(raw.history_hours) : 24,
    statsDays: Number(raw.stats_days) > 0 ? Number(raw.stats_days) : 7,
    cards,
    probeTimeout: Number.isFinite(timeout) && timeout >= 0 ? timeout : PROBE_TIMEOUT,
    areas: raw.areas && typeof raw.areas === "object" ? raw.areas : {},
    dashboard: raw.dashboard_path || currentDashboardPath(),
    // Overview density. Sparklines are OFF by default: eleven full-width
    // 56px graphs read as a wall of long coloured lines rather than as
    // sparklines, and the per-room views already carry real history.
    sparklines: raw.sparklines === true,
    // Threshold-coloured sparklines only on request — with the house sitting
    // at 80-85 °F the whole ladder lands in amber/red, so every line comes
    // out the same alarming colour and the colour stops meaning anything.
    sparklineThresholds: raw.sparkline_thresholds === true,
    // 12 = full row of its section. Half-width by default so the heroes tile
    // two-up instead of stacking eleven full-bleed rows.
    heroColumns:
      Number.isFinite(heroColumns) && heroColumns >= 3 && heroColumns <= 12
        ? heroColumns
        : 6,
    // "compact" keeps the secondary to value + coverage so it fits a
    // half-width card; "full" adds the min-max range and the trend arrow.
    detail: raw.detail === "full" ? "full" : "compact",
  };
}

function currentDashboardPath() {
  if (typeof location === "undefined" || !location.pathname) {
    return DEFAULT_DASHBOARD;
  }
  const first = location.pathname.split("/").filter(Boolean)[0];
  return first || DEFAULT_DASHBOARD;
}

/* Which custom elements this browser actually has, resolved once per load. */
async function probeCards(names, timeout) {
  if (typeof customElements === "undefined" || !customElements.whenDefined) {
    return new Set();
  }
  const found = new Set();
  await Promise.all(
    names.map(async (name) => {
      if (customElements.get && customElements.get(name)) {
        found.add(name);
        return;
      }
      const ready = await Promise.race([
        customElements.whenDefined(name).then(() => name),
        new Promise((resolve) => setTimeout(() => resolve(null), timeout)),
      ]);
      if (ready) found.add(ready);
    })
  );
  return found;
}

async function capabilities(opts) {
  if (opts.cards === "native") return new Set();
  if (opts.cards === "mushroom") return new Set(PROBE);
  return probeCards(PROBE, opts.probeTimeout);
}

/* The only way this file reads a contract attribute: absent means fall back,
 * never means broken. */
function attr(state, name, fallbackFn) {
  const value =
    state && state.attributes ? state.attributes[name] : undefined;
  if (value !== undefined && value !== null) return value;
  return fallbackFn ? fallbackFn(state) : undefined;
}

/* The integration owns the switch; the hand-made helper is the old name. */
function resolveToggle(hass) {
  for (const id of TOGGLES) if (hass.states[id]) return id;
  return null;
}

/* ------------------------------------------------------------------ bands */

function round3(value) {
  return Math.round(value * 1000) / 1000;
}

/* Half-open bands become strict numeric_state windows. A band's `above` is
 * the previous band's `below` minus one epsilon, so an inclusive upper bound
 * (84 belongs to amber, not red) can never match two bands at once. */
function bandCuts(table) {
  const cuts = [];
  let previousBelow = null;
  table.bands.forEach((band, index) => {
    const last = index === table.bands.length - 1;
    const below = last
      ? null
      : round3(band.maxInclusive ? band.max + EPS : band.max);
    const above = previousBelow === null ? null : round3(previousBelow - EPS);
    cuts.push({ band, above, below });
    previousBelow = below;
  });
  return cuts;
}

function bandVisibility(entityId, cut) {
  const condition = { condition: "numeric_state", entity: entityId };
  if (cut.above !== null) condition.above = cut.above;
  if (cut.below !== null) condition.below = cut.below;
  return [condition];
}

/* Same ladder the curated dashboard uses, emitted from the table. */
function iconColorTemplate(entityId, table) {
  if (!table.bands.length) return null;
  const chain = table.bands
    .map((band, index) => {
      if (index === table.bands.length - 1) return `{% else %}${band.color}`;
      const op = band.maxInclusive ? "<=" : "<";
      const keyword = index === 0 ? "if" : "elif";
      return `{% ${keyword} t ${op} ${band.max} %}${band.color}`;
    })
    .join("");
  return (
    `{% set s = states('${entityId}') %}` +
    `{% if s in ['unavailable', 'unknown'] %}disabled` +
    `{% else %}{% set t = s | float(0) %}${chain}{% endif %}{% endif %}`
  );
}

function badgeIconTemplate(entityId, table) {
  if (table.hotAbove === null) return null;
  return (
    `{% set s = states('${entityId}') %}` +
    `{% if s not in ['unavailable', 'unknown'] and s | float(0) > ` +
    `${table.hotAbove} %}${table.hotIcon}{% endif %}`
  );
}

function colorThresholds(table) {
  if (!table.bands.length) return null;
  const out = [{ value: 0, color: table.bands[0].hex }];
  for (let i = 1; i < table.bands.length; i += 1) {
    out.push({ value: table.bands[i].min, color: table.bands[i].hex });
  }
  return out;
}

/* ------------------------------------------------------------------ names */

function escapeRe(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/* "Bedroom ITH-11-B 0440 Temperature" -> "ITH-11-B 0440". Falls back to the
 * device name and then to the untouched friendly name. */
function shortName(friendlyName, areaName, taken, deviceName) {
  const original = String(friendlyName || "").trim();
  let name = original;
  if (areaName) {
    name = name.replace(new RegExp(`^${escapeRe(areaName)}[\\s_-]+`, "i"), "");
  }
  name = name.replace(/[\s_-]*(temperature|temp)$/i, "");
  name = name.replace(/[\s_·-]+$/, "").replace(/\s+/g, " ").trim();
  /* "kitchen_thermometer Temperature" -> "Thermometer", but never touch names
   * carrying digits or separators ("esp32-thermal", "temp1_1B80"). */
  if (/^[a-z][a-z ]*$/.test(name)) name = name[0].toUpperCase() + name.slice(1);
  const used = (candidate) =>
    Boolean(candidate) && taken.has(candidate.toLowerCase());
  if (name.length >= 3 && !used(name)) return name;
  if (deviceName && !used(deviceName)) return deviceName;
  if (original && !used(original)) return original;
  return original || name;
}

function areaIcon(areaId, name) {
  const key = String(areaId || "").toLowerCase();
  if (ICON_FALLBACK[key]) return ICON_FALLBACK[key];
  const words = String(name || "").toLowerCase();
  if (words.includes("bath") || words.includes("shower")) return "mdi:shower";
  return "mdi:home-thermometer-outline";
}

/* ------------------------------------------------------------------ rooms */

function isNumericState(state) {
  if (!state || OFFLINE.includes(state.state)) return false;
  return Number.isFinite(parseFloat(state.state));
}

function deviceNameOf(hass, entityId) {
  const reg = hass.entities ? hass.entities[entityId] : undefined;
  const device =
    reg && reg.device_id && hass.devices ? hass.devices[reg.device_id] : undefined;
  return device ? device.name_by_user || device.name || null : null;
}

function memberInfo(hass, ids, areaName, offlineSet, taken) {
  return ids.map((id) => {
    const state = hass.states[id];
    const friendly =
      (state && state.attributes && state.attributes.friendly_name) || id;
    const name = shortName(friendly, areaName, taken, deviceNameOf(hass, id));
    taken.add(String(name).toLowerCase());
    return {
      id,
      name,
      state,
      unit:
        (state && state.attributes && state.attributes.unit_of_measurement) ||
        null,
      offline: offlineSet.has(id),
      reporting: isNumericState(state),
    };
  });
}

function areaPlan(areaId, opts, deviceClass) {
  const base = AREA_DEFAULTS[areaId] || { group: "inside", band: "default" };
  const override = opts.areas[areaId] || {};
  const group = GROUP_IDS.includes(override.group) ? override.group : base.group;
  let band = BANDS[override.band] ? override.band : base.band;
  if (deviceClass !== "temperature") band = "none";
  return { group, band };
}

function collectRooms(hass, deviceClass, opts) {
  const rooms = [];
  for (const state of Object.values(hass.states)) {
    const reg = hass.entities ? hass.entities[state.entity_id] : undefined;
    if (!reg || reg.platform !== PLATFORM) continue;
    const attributes = state.attributes || {};
    if ((attributes.device_class || "") !== deviceClass) continue;
    const areaId =
      reg.area_id ||
      (reg.device_id && hass.devices && hass.devices[reg.device_id]
        ? hass.devices[reg.device_id].area_id
        : undefined);
    const area = areaId && hass.areas ? hass.areas[areaId] : undefined;
    const areaName =
      (area && area.name) ||
      (attributes.friendly_name || state.entity_id).replace(
        / Average .*$/,
        ""
      );
    const members = attr(state, "sensors", () => []) || [];
    const excluded = attr(state, "excluded", () => []) || [];
    const offlineList = attr(state, "offline", () =>
      members.filter((id) => !isNumericState(hass.states[id]))
    );
    const offlineSet = new Set(offlineList || []);
    const memberCount = attr(state, "member_count", () => members.length);
    const reporting = attr(
      state,
      "reporting",
      () => members.filter((id) => isNumericState(hass.states[id])).length
    );
    const plan = areaPlan(areaId, opts, deviceClass);
    /* one namespace per room, so an exclusion cannot shadow a member */
    const taken = new Set();
    rooms.push({
      avg: state.entity_id,
      areaId: areaId || state.entity_id,
      area: areaName,
      icon: (area && area.icon) || areaIcon(areaId, areaName),
      state,
      unit: attributes.unit_of_measurement || null,
      members: memberInfo(hass, members, areaName, offlineSet, taken),
      excluded: memberInfo(hass, excluded, areaName, new Set(), taken),
      offline: offlineList || [],
      memberCount,
      reporting,
      hasSummary: attr(state, "summary", () => null) !== null,
      group: plan.group,
      band: plan.band,
      deviceClass,
    });
  }
  rooms.sort((a, b) => a.area.localeCompare(b.area));
  return rooms;
}

function roomPath(room) {
  const prefix = room.deviceClass === "temperature" ? "" : `${room.deviceClass}-`;
  return `room-${prefix}${room.areaId}`;
}

function roomNavigation(room, opts) {
  return {
    action: "navigate",
    navigation_path: `/${opts.dashboard}/${roomPath(room)}`,
  };
}

/* ------------------------------------------------------------------- hero */

/* Live coverage line: "83.7 °F · 2 of 9 sensors · 80.6–85.8 · ↑ rising".
 * The summary attribute is preferred, but the same phrase is rebuilt in Jinja
 * when an older integration build does not publish it. */
function heroSecondary(room, detail) {
  const e = room.avg;
  return (
    `{% set s = states('${e}') %}` +
    `{% set u = state_attr('${e}', 'unit_of_measurement') or '' %}` +
    `{% set mc = state_attr('${e}', 'member_count') | int(0) %}` +
    `{% set rp = state_attr('${e}', 'reporting') | int(0) %}` +
    `{% set sm = state_attr('${e}', 'summary') %}` +
    `{% if sm is none %}` +
    `{% if rp == 0 %}{% set sm = 'no sensors reporting' %}` +
    `{% elif mc == 1 %}{% set sm = '1 sensor' %}` +
    `{% elif rp < mc %}{% set sm = rp ~ ' of ' ~ mc ~ ' sensors' %}` +
    `{% else %}{% set sm = mc ~ ' sensors' %}{% endif %}{% endif %}` +
    `{% if s in ['unavailable', 'unknown'] %}no data · {{ sm }}` +
    `{% else %}{{ s | float(0) | round(1) }} {{ u }} · {{ sm }}` +
    // The range and the trend arrow are real information but they push the
    // secondary past what a half-width card can show, and it truncates
    // mid-number. They live on the room's own view; opt in with detail: full.
    (detail === "full"
      ? `{% set mn = state_attr('${e}', 'minimum') %}` +
        `{% set mx = state_attr('${e}', 'maximum') %}` +
        `{% if mc > 2 and mn is not none and mx is not none %}` +
        ` · {{ mn | round(1) }}–{{ mx | round(1) }}{% endif %}` +
        `{% set tr = state_attr('${e}', 'trend') %}` +
        `{% if tr == 'rising' %} · ↑ rising` +
        `{% elif tr == 'falling' %} · ↓ falling{% endif %}`
      : "") +
    `{% endif %}`
  );
}

function mushroomHero(room, ctx) {
  const table = BANDS[room.band];
  const color = iconColorTemplate(room.avg, table);
  const badge = badgeIconTemplate(room.avg, table);
  const card = {
    type: "custom:mushroom-template-card",
    entity: room.avg,
    primary: room.area,
    secondary: heroSecondary(room, ctx.opts.detail),
    icon: room.icon,
    tap_action: roomNavigation(room, ctx.opts),
    hold_action: { action: "more-info" },
  };
  if (color) card.icon_color = color;
  if (badge) {
    card.badge_icon = badge;
    card.badge_color = "red";
  }
  return card;
}

function miniGraph(room, ctx) {
  const thresholds = colorThresholds(BANDS[room.band]);
  const card = {
    type: "custom:mini-graph-card",
    entities: [{ entity: room.avg }],
    hours_to_show: ctx.opts.hours,
    points_per_hour: 3,
    line_width: 2,
    height: 56,
    smoothing: true,
    show: {
      name: false,
      icon: false,
      state: false,
      legend: false,
      labels: false,
      points: false,
      fill: "fade",
    },
    min_bound_range: 8,
    card_mod: { style: "ha-card { margin-top: -8px; }" },
  };
  if (thresholds && ctx.opts.sparklineThresholds) {
    card.color_thresholds = thresholds;
    card.color_thresholds_transition = "smooth";
  } else {
    // One quiet line. The hero's icon already carries the band colour, and
    // repeating it across every sparkline just fills the page with red.
    card.line_color = "var(--secondary-text-color)";
  }
  return card;
}

/* Core-only hero: one tile per band, exactly one of which is ever visible.
 * No features on the variants — hidden cards are still instantiated, and a
 * trend-graph feature on five variants would multiply the history fetches. */
function nativeHero(room, ctx) {
  const table = BANDS[room.band];
  const base = {
    type: "tile",
    entity: room.avg,
    name: room.area,
    icon: room.icon,
    state_content: room.hasSummary ? ["state", "summary"] : ["state"],
    grid_options: { columns: ctx.opts.heroColumns },
    tap_action: roomNavigation(room, ctx.opts),
    hold_action: { action: "more-info" },
  };
  if (!table.bands.length) return [base];
  const cards = bandCuts(table).map((cut) => ({
    ...base,
    color: cut.band.color,
    visibility: bandVisibility(room.avg, cut),
  }));
  cards.push({
    ...base,
    color: "grey",
    visibility: [{ condition: "state", entity: room.avg, state: OFFLINE }],
  });
  return cards;
}

function heroCards(room, ctx) {
  const grid = { columns: ctx.opts.heroColumns };
  if (!ctx.caps.has("mushroom-template-card")) return nativeHero(room, ctx);
  const stack = [mushroomHero(room, ctx)];
  if (ctx.opts.sparklines && ctx.caps.has("mini-graph-card")) {
    stack.push(miniGraph(room, ctx));
  }
  if (stack.length > 1 && ctx.caps.has("stack-in-card")) {
    return [
      {
        type: "custom:stack-in-card",
        mode: "vertical",
        grid_options: grid,
        cards: stack,
      },
    ];
  }
  return stack.map((card) => ({ ...card, grid_options: grid }));
}

/* ---------------------------------------------------------- member tiles */

function memberVisibility(member, ctx) {
  if (!ctx.toggle) return null;
  return [
    {
      condition: "or",
      conditions: [
        { condition: "state", entity: member.id, state_not: OFFLINE },
        { condition: "state", entity: ctx.toggle, state: "on" },
      ],
    },
  ];
}

/* °C member inside a °F average: convert live, in Jinja, and show both. */
function conversionSecondary(member, targetUnit) {
  const e = member.id;
  const expression =
    targetUnit === "°F"
      ? `(v | float(0) * 1.8 + 32) | round(1)`
      : `((v | float(0) - 32) / 1.8) | round(1)`;
  return (
    `{% set v = states('${e}') %}` +
    `{% if v in ['unavailable', 'unknown'] or not is_number(v) %}no data` +
    `{% else %}{{ ${expression} }} ${targetUnit} ` +
    `({{ v | float(0) | round(1) }} ${member.unit}){% endif %}`
  );
}

function memberTile(member, room, ctx) {
  const mismatched =
    member.unit && room.unit && member.unit !== room.unit &&
    ["°C", "°F"].includes(member.unit) && ["°C", "°F"].includes(room.unit);
  const visibility = memberVisibility(member, ctx);
  let card;
  if (mismatched && ctx.caps.has("mushroom-template-card")) {
    card = {
      type: "custom:mushroom-template-card",
      entity: member.id,
      primary: member.name,
      secondary: conversionSecondary(member, room.unit),
      icon: "mdi:thermometer",
      tap_action: { action: "more-info" },
    };
  } else {
    card = {
      type: "tile",
      entity: member.id,
      name: mismatched ? `${member.name} (${member.unit})` : member.name,
      state_content: ["state", "last_changed"],
    };
  }
  if (visibility) card.visibility = visibility;
  return card;
}

function excludedTile(member) {
  return {
    type: "tile",
    entity: member.id,
    name: member.name,
    icon: "mdi:sigma-lower",
    color: "disabled",
    vertical: true,
    state_content: ["state"],
  };
}

/* The reveal control, shown only while this room hides something. */
function revealTile(room, ctx) {
  const anyOffline = room.members.map((m) => ({
    condition: "state",
    entity: m.id,
    state: OFFLINE,
  }));
  return {
    type: "tile",
    entity: ctx.toggle,
    name: "Offline sensors",
    icon: "mdi:lan-disconnect",
    color: "grey",
    hide_state: true,
    tap_action: { action: "toggle" },
    visibility: [{ condition: "or", conditions: anyOffline }],
  };
}

/* Entity badges only — button heading badges are too new to rely on here. */
function revealBadges(room, ctx) {
  const anyOffline = room.members.map((m) => ({
    condition: "state",
    entity: m.id,
    state: OFFLINE,
  }));
  // Never gate on generate-time offline status: generate() runs once per page
  // load, so a member that dies later must still be able to surface itself.
  // The visibility conditions below are evaluated live and do that on their own.
  if (!ctx.toggle || !room.members.length) return [];
  return [
    {
      type: "entity",
      entity: ctx.toggle,
      name: "Offline",
      icon: "mdi:lan-disconnect",
      color: "grey",
      show_name: true,
      tap_action: { action: "toggle" },
      visibility: [
        { condition: "or", conditions: anyOffline },
        { condition: "state", entity: ctx.toggle, state: "off" },
      ],
    },
    {
      type: "entity",
      entity: ctx.toggle,
      name: "Hide offline",
      icon: "mdi:eye-off-outline",
      color: "blue",
      show_name: true,
      tap_action: { action: "toggle" },
      visibility: [
        { condition: "or", conditions: anyOffline },
        { condition: "state", entity: ctx.toggle, state: "on" },
      ],
    },
  ];
}

/* ----------------------------------------------------------------- status */

/* Never claims health it does not have: offline rooms first, worst first,
 * then stale, then batteries, inside a real ha-alert. */
function statusTemplate(insideAreaIds, outdoorAvgIds) {
  const inside = JSON.stringify(insideAreaIds);
  const outdoor = JSON.stringify(outdoorAvgIds);
  return `{%- set inside = ${inside} -%}
{%- set outdoor = ${outdoor} -%}
{%- set ns = namespace(temps=[], dark=[], off=[], stale=[], batt=[], rows=[], total=0, warm='', warmv=-999, cold='', coldv=999, unit='', insum=0, inn=0) -%}
{%- for e in integration_entities('area_averages') | sort -%}
{%- if state_attr(e, 'device_class') == 'temperature' -%}{%- set ns.temps = ns.temps + [e] -%}{%- endif -%}
{%- endfor -%}
{%- for e in ns.temps -%}
{%- set name = area_name(e) or e -%}
{%- set mc = state_attr(e, 'member_count') | int(0) -%}
{%- set rp = state_attr(e, 'reporting') | int(0) -%}
{%- set offl = state_attr(e, 'offline') -%}
{%- set offn = (offl | count) if offl is not none else (mc - rp) -%}
{%- set ns.total = ns.total + mc -%}
{%- if rp == 0 -%}{%- set ns.dark = ns.dark + [name ~ ' — no sensors reporting'] -%}
{%- elif offn > 0 -%}{%- set ns.off = ns.off + [(offn, name ~ ' — ' ~ offn ~ ' of ' ~ mc ~ ' sensors offline')] -%}{%- endif -%}
{%- set st = state_attr(e, 'stale') or [] -%}
{%- if st | count > 0 -%}{%- set ns.stale = ns.stale + [name ~ ' — ' ~ (st | count) ~ ' sensor' ~ ('s' if st | count > 1 else '') ~ ' stale'] -%}{%- endif -%}
{%- for i in state_attr(e, 'issues') or [] -%}
{%- if 'battery' in i -%}{%- set ns.batt = ns.batt + [name ~ ' — ' ~ i] -%}{%- endif -%}
{%- endfor -%}
{%- set v = states(e) -%}
{%- if is_number(v) and area_id(e) in inside -%}
{%- set f = v | float(0) -%}
{%- if ns.unit == '' -%}{%- set ns.unit = state_attr(e, 'unit_of_measurement') or '' -%}{%- endif -%}
{%- if f > ns.warmv -%}{%- set ns.warmv = f -%}{%- set ns.warm = name -%}{%- endif -%}
{%- if f < ns.coldv -%}{%- set ns.coldv = f -%}{%- set ns.cold = name -%}{%- endif -%}
{%- set ns.insum = ns.insum + f -%}{%- set ns.inn = ns.inn + 1 -%}
{%- endif -%}
{%- endfor -%}
{%- set ns.rows = ns.dark -%}
{%- for r in ns.off | sort(reverse=true) -%}{%- set ns.rows = ns.rows + [r[1]] -%}{%- endfor -%}
{%- set ns.rows = ns.rows + ns.stale + ns.batt -%}
{%- set out = namespace(v=none, house='') -%}
{%- for o in outdoor -%}{%- if out.v is none and is_number(states(o)) -%}{%- set out.v = states(o) | float(0) -%}{%- endif -%}{%- endfor -%}
{%- if ns.warm != '' -%}{%- set out.house = 'warmest ' ~ ns.warm ~ ' ' ~ (ns.warmv | round(1)) ~ ' ' ~ ns.unit ~ ' · coldest ' ~ ns.cold ~ ' ' ~ (ns.coldv | round(1)) ~ ' ' ~ ns.unit -%}{%- endif -%}
{%- if ns.inn > 0 and out.v is not none -%}
{%- set ia = ns.insum / ns.inn -%}
{%- set d = ia - out.v -%}
{%- set out.house = out.house ~ ' · inside ' ~ (ia | round(1)) ~ ' ' ~ ns.unit ~ ' · outside ' ~ (out.v | round(1)) ~ ' ' ~ ns.unit ~ ' · ' ~ ((d | abs) | round(1)) ~ ' ' ~ ns.unit ~ (' warmer' if d >= 0 else ' cooler') -%}
{%- endif -%}
{% if ns.rows | count > 0 %}> [!{{ 'CAUTION' if ns.dark | count > 0 else 'WARNING' }}]
{% for r in ns.rows %}> {{ r }}
{% endfor %}
{{ out.house }}
{% else %}{{ ns.temps | count }} area{{ 's' if ns.temps | count != 1 else '' }} reporting · {{ ns.total }} sensor{{ 's' if ns.total != 1 else '' }}{{ ' · ' ~ out.house if out.house != '' else '' }}
{% endif %}`;
}

function statusSection(rooms, ctx) {
  const inside = rooms.filter((r) => r.group === "inside").map((r) => r.areaId);
  const outdoor = rooms
    .filter((r) => r.group === "outdoors")
    .map((r) => r.avg);
  return {
    type: "grid",
    column_span: 3,
    cards: [
      {
        type: "heading",
        heading: "Status",
        heading_style: "title",
        icon: "mdi:heart-pulse",
        tap_action: {
          action: "navigate",
          navigation_path: `/${ctx.opts.dashboard}/trends`,
        },
      },
      {
        type: "markdown",
        content: statusTemplate(inside, outdoor),
        grid_options: { columns: "full" },
      },
    ],
  };
}

/* ------------------------------------------------------------------ views */

function groupSections(rooms, ctx, cardsFor) {
  const sections = [];
  for (const group of GROUPS) {
    const members = rooms.filter((room) => room.group === group.id);
    if (!members.length) continue;
    const cards = [
      {
        type: "heading",
        heading: group.title,
        heading_style: "title",
        icon: group.icon,
      },
    ];
    for (const room of members) cards.push(...cardsFor(room));
    // Inside holds most of the house, so on a 3-column desktop it would tower
    // over two short columns and leave the fold half empty. Let the biggest
    // group take two columns once it is clearly the heaviest.
    const section = { type: "grid", cards };
    const sizes = GROUPS.map((g) => rooms.filter((r) => r.group === g.id).length);
    const heaviest = Math.max(...sizes);
    const soleHeaviest = sizes.filter((n) => n === heaviest).length === 1;
    if (
      soleHeaviest &&
      members.length === heaviest &&
      members.length >= rooms.length - members.length
    ) {
      section.column_span = 2;
    }
    sections.push(section);
  }
  return sections;
}

function viewBadges(rooms, ctx) {
  const badges = [];
  for (const room of rooms.filter((r) => r.group === "outdoors")) {
    badges.push({
      type: "entity",
      entity: room.avg,
      name: room.area,
      icon: room.icon,
      show_name: true,
    });
  }
  // One badge per affected room, each visible only on ITS OWN members going
  // dark. A single "worst room" badge with a dashboard-wide visibility OR would
  // keep naming the room it picked at generate time while pointing at another
  // room's outage — the one element on the first screen that could state
  // something false. Capped so a bad day cannot flood the header.
  const affected = rooms
    .map((room) => ({ room, dark: room.members.filter((m) => m.offline) }))
    .filter((entry) => entry.dark.length)
    .sort((a, b) => b.dark.length - a.dark.length)
    .slice(0, MAX_OFFLINE_BADGES);
  for (const { room } of affected) {
    badges.push({
      type: "entity",
      entity: room.avg,
      name: room.area,
      icon: "mdi:lan-disconnect",
      color: "red",
      show_name: true,
      tap_action: roomNavigation(room, ctx.opts),
      visibility: [
        {
          condition: "or",
          conditions: room.members.map((member) => ({
            condition: "state",
            entity: member.id,
            state: OFFLINE,
          })),
        },
      ],
    });
  }
  return badges;
}

function roomsView(deviceClass, rooms, ctx) {
  const meta = CLASS_META[deviceClass] || {
    title: deviceClass,
    path: deviceClass.replace(/[^a-z0-9]+/g, "-"),
    icon: "mdi:chart-box-outline",
  };
  const sections = [statusSection(rooms, ctx)].concat(
    groupSections(rooms, ctx, (room) => heroCards(room, ctx))
  );
  const badges = viewBadges(rooms, ctx);
  return {
    title: meta.title,
    path: meta.path,
    icon: meta.icon,
    type: "sections",
    max_columns: 3,
    top_margin: true,
    dense_section_placement: false,
    ...(badges.length ? { badges } : {}),
    sections,
  };
}

function roomSubview(room, ctx) {
  const badges = revealBadges(room, ctx);
  const sections = [
    {
      type: "grid",
      cards: [
        {
          type: "heading",
          heading: room.area,
          heading_style: "title",
          icon: room.icon,
          ...(badges.length ? { badges } : {}),
        },
        ...heroCards(room, ctx),
      ],
    },
  ];

  if (room.members.length) {
    const cards = [
      {
        type: "heading",
        heading_style: "subtitle",
        heading: `Sensors (${room.members.length})`,
        icon: "mdi:thermometer",
      },
      ...room.members.map((member) => memberTile(member, room, ctx)),
    ];
    // Emitted whenever the room has members at all — revealTile carries its own
    // live OR-over-members visibility, so it appears exactly when something is
    // actually offline, including members that only go dark after page load.
    if (ctx.toggle) cards.push(revealTile(room, ctx));
    sections.push({ type: "grid", cards });
  }

  if (room.excluded.length) {
    sections.push({
      type: "grid",
      cards: [
        {
          type: "heading",
          heading_style: "subtitle",
          heading: `Not in average (${room.excluded.length})`,
          icon: "mdi:sigma",
        },
        ...room.excluded.map((member) => excludedTile(member)),
      ],
    });
  }

  sections.push({
    type: "grid",
    cards: [
      {
        type: "heading",
        heading_style: "subtitle",
        heading: `Last ${ctx.opts.hours} h`,
        icon: "mdi:chart-line",
      },
      {
        type: "history-graph",
        hours_to_show: ctx.opts.hours,
        entities: [{ entity: room.avg }].concat(
          room.members
            .filter((member) => member.reporting)
            .map((member) => ({ entity: member.id, name: member.name }))
        ),
        grid_options: { columns: "full" },
      },
    ],
  });

  return {
    title: room.area,
    path: roomPath(room),
    icon: room.icon,
    subview: true,
    type: "sections",
    max_columns: 2,
    sections,
  };
}

/* Long-term statistics only: one unit per chart by construction, and a core
 * card carries no dependency risk on the longest-lived view. */
function trendsView(rooms, ctx) {
  const overview = {
    type: "grid",
    column_span: 3,
    cards: [
      {
        type: "heading",
        heading: `All rooms · hourly mean · ${ctx.opts.statsDays} days`,
        heading_style: "title",
        icon: "mdi:chart-line",
      },
      {
        type: "statistics-graph",
        entities: rooms.map((room) => room.avg),
        stat_types: ["mean"],
        period: "hour",
        days_to_show: ctx.opts.statsDays,
        chart_type: "line",
        grid_options: { columns: "full" },
      },
    ],
  };
  const sections = [overview].concat(
    groupSections(rooms, ctx, (room) => [
      {
        type: "statistics-graph",
        title: room.area,
        entities: [room.avg],
        stat_types: ["min", "mean", "max"],
        period: "hour",
        days_to_show: ctx.opts.statsDays,
        chart_type: "line",
        hide_legend: true,
        grid_options: { columns: 6 },
      },
    ])
  );
  return {
    title: "Trends",
    path: "trends",
    icon: "mdi:chart-line",
    type: "sections",
    max_columns: 3,
    sections,
  };
}

class AreaTemperaturesStrategy {
  static async generate(config, hass) {
    const opts = readOptions(config);
    const ctx = {
      opts,
      caps: await capabilities(opts),
      toggle: resolveToggle(hass),
    };

    const classes = new Set();
    for (const state of Object.values(hass.states)) {
      const reg = hass.entities ? hass.entities[state.entity_id] : undefined;
      const deviceClass = (state.attributes || {}).device_class;
      if (reg && reg.platform === PLATFORM && deviceClass) {
        classes.add(deviceClass);
      }
    }
    const ordered = [
      "temperature",
      ...[...classes].filter((c) => c !== "temperature").sort(),
    ];

    const views = [];
    for (const deviceClass of ordered) {
      if (!classes.has(deviceClass)) continue;
      const rooms = collectRooms(hass, deviceClass, opts);
      if (!rooms.length) continue;
      views.push(roomsView(deviceClass, rooms, ctx));
      if (deviceClass === "temperature") views.push(trendsView(rooms, ctx));
      for (const room of rooms) views.push(roomSubview(room, ctx));
    }
    if (!views.length) {
      views.push({
        title: "Rooms",
        path: "rooms",
        type: "sections",
        sections: [statusSection([], ctx)],
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
