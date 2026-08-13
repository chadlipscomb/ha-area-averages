/* Area Averages dashboard strategy.
 *
 * Generates the whole "Area Temperatures" dashboard at load time from the live
 * area_averages sensors, so the page tracks reality — new sensors, new areas,
 * exclusions — with no regeneration step.
 *
 * Shape: a Rooms view of one section per area, each holding the area name, the
 * temperature at display size, a button that reveals that room's sensors and a
 * button that reveals only the dark ones; plus a Trends view of core
 * statistics-graph cards, which is where all history lives.
 *
 * Look: Liquid Glass. Each card is a translucent blurred pane with a specular
 * rim, expressed in color-mix() against HA's theme variables so one definition
 * serves light and dark. Needs card-mod; without it the same structure renders
 * as ordinary HA cards.
 *
 * Two rules this file exists to keep:
 *   1. Colour never comes from JS. generate() runs once per page load, so a
 *      colour computed here would freeze until reload. The mushroom path emits
 *      a Jinja ladder; the core path emits one tile per band with numeric_state
 *      visibility. Both come from the single BANDS table below.
 *   2. Nothing is gated on generate-time state. Every reveal is a live
 *      visibility condition, so a sensor that dies while the page is open
 *      surfaces its own button instead of vanishing silently.
 *
 * Registered as a lovelace resource (/local/area_averages_strategy.js).
 * Dashboard config is just: {"strategy": {"type": "custom:area-temperatures"}}
 * Options: glass (true), detail ("compact"|"full"), cards
 * ("auto"|"mushroom"|"native"), stats_days (7), probe_timeout_ms (1500),
 * dashboard_path, areas ({"<area_id>": {"group": "...", "band": "..."}}).
 *
 * NOTE: do not "take control" of the dashboard in the UI editor — that
 * freezes the current snapshot into a static config.
 */

const VERSION = "3.0.0";
const PLATFORM = "area_averages";
const OFFLINE = ["unavailable", "unknown"];
const MAX_OFFLINE_BADGES = 3;
const OFFLINE_TOGGLES = [
  "switch.area_averages_show_offline",
  "input_boolean.show_offline_sensors",
];
const SENSOR_TOGGLES = [
  "switch.area_averages_show_sensors",
  "input_boolean.show_all_sensors",
];
const PROBE = ["mushroom-template-card", "card-mod"];
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
  return {
    statsDays: Number(raw.stats_days) > 0 ? Number(raw.stats_days) : 7,
    cards,
    probeTimeout: Number.isFinite(timeout) && timeout >= 0 ? timeout : PROBE_TIMEOUT,
    areas: raw.areas && typeof raw.areas === "object" ? raw.areas : {},
    dashboard: raw.dashboard_path || currentDashboardPath(),
    // The Liquid Glass treatment. Needs card-mod; set false for flat HA cards.
    glass: raw.glass !== false,
    // "compact" is the temperature and its coverage; "full" adds the range
    // and a trend arrow to the small line under it.
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

/* The integration owns these switches; the hand-made helpers are the old
 * names, kept so an existing install does not lose its reveal controls. */
function resolveToggle(hass, candidates) {
  for (const id of candidates) if (hass.states[id]) return id;
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

/* ------------------------------------------------------------------ glass */

/* Liquid Glass. One translucent, blurred pane per card, lifted off the page
 * by a soft shadow and edged with a specular highlight along its top rim.
 *
 * Everything is expressed against HA's own theme variables through
 * color-mix(), so a single definition works in light and dark without
 * hardcoding a palette: the pane borrows the theme's card colour, thins it to
 * let the background through, and takes its rim and text tint from the
 * theme's text colour. Nothing here changes layout — if card-mod is missing
 * the cards simply render as ordinary HA cards. */

const GLASS_RADIUS = 22;

function glassPane({ radius = GLASS_RADIUS, tint = 62, lift = 1 } = {}) {
  return `
    ha-card {
      background: color-mix(in srgb, var(--card-background-color) ${tint}%, transparent);
      -webkit-backdrop-filter: blur(22px) saturate(180%);
      backdrop-filter: blur(22px) saturate(180%);
      border: 1px solid color-mix(in srgb, var(--primary-text-color) 10%, transparent);
      border-radius: ${radius}px;
      box-shadow:
        0 ${2 * lift}px ${10 * lift}px color-mix(in srgb, #000 ${8 * lift}%, transparent),
        0 ${8 * lift}px ${30 * lift}px color-mix(in srgb, #000 ${6 * lift}%, transparent),
        inset 0 1px 0 color-mix(in srgb, #fff 40%, transparent);
      transition: transform 180ms ease, box-shadow 180ms ease;
    }
    ha-card:hover {
      transform: translateY(-1px);
      box-shadow:
        0 ${3 * lift}px ${14 * lift}px color-mix(in srgb, #000 10%, transparent),
        0 ${12 * lift}px ${36 * lift}px color-mix(in srgb, #000 8%, transparent),
        inset 0 1px 0 color-mix(in srgb, #fff 55%, transparent);
    }
  `;
}

/* The temperature is the point of the card, so it gets display-sized type.
 * Tabular figures stop the number jittering as it changes. */
const GLASS_READING = `
  .primary {
    font-size: 34px;
    font-weight: 600;
    letter-spacing: -0.025em;
    line-height: 1.15;
    font-variant-numeric: tabular-nums;
  }
  .secondary {
    font-size: 12.5px;
    font-weight: 500;
    letter-spacing: 0.01em;
    opacity: 0.62;
  }
`;

const GLASS_PILL_TEXT = `
  .primary { font-size: 13.5px; font-weight: 600; letter-spacing: 0.01em; }
  .secondary { font-size: 11px; opacity: 0.55; }
`;

/* card-mod takes a plain string for the card itself, or a map when a shadow
 * root has to be reached. Absent card-mod, both are inert extra keys. */
function withGlass(card, ctx, { inner, radius, tint, lift } = {}) {
  if (!ctx.caps.has("card-mod") || !ctx.opts.glass) return card;
  const pane = glassPane({ radius, tint, lift });
  card.card_mod = inner
    ? { style: { ".": pane, "mushroom-state-info$": inner } }
    : { style: pane };
  return card;
}

/* --------------------------------------------------------------- the value */

/* "4 of 6 sensors" — the attribute when the integration publishes it, the
 * same phrase rebuilt in Jinja when it does not. */
function coverageExpr(entityId) {
  return (
    `{% set mc = state_attr('${entityId}', 'member_count') | int(0) %}` +
    `{% set rp = state_attr('${entityId}', 'reporting') | int(0) %}` +
    `{% set sm = state_attr('${entityId}', 'summary') %}` +
    `{% if sm is none %}` +
    `{% if rp == 0 %}{% set sm = 'no sensors reporting' %}` +
    `{% elif mc == 1 %}{% set sm = '1 sensor' %}` +
    `{% elif rp < mc %}{% set sm = rp ~ ' of ' ~ mc ~ ' sensors' %}` +
    `{% else %}{% set sm = mc ~ ' sensors' %}{% endif %}{% endif %}`
  );
}

/* Big line: the temperature, nothing else competing with it. */
function readingPrimary(room) {
  const e = room.avg;
  return (
    `{% set s = states('${e}') %}` +
    `{% set u = state_attr('${e}', 'unit_of_measurement') or '' %}` +
    `{% if s in ['unavailable', 'unknown'] %}—` +
    `{% else %}{{ s | float(0) | round(1) }}{{ u }}{% endif %}`
  );
}

/* Small line: coverage, plus the range and trend when asked for. */
function readingSecondary(room, detail) {
  const e = room.avg;
  return (
    coverageExpr(e) +
    `{% set s = states('${e}') %}` +
    `{% if s in ['unavailable', 'unknown'] %}no data · {{ sm }}` +
    `{% else %}{{ sm }}` +
    (detail === "full"
      ? `{% set mn = state_attr('${e}', 'minimum') %}` +
        `{% set mx = state_attr('${e}', 'maximum') %}` +
        `{% if mn is not none and mx is not none and mn != mx %}` +
        ` · {{ mn | round(1) }}–{{ mx | round(1) }}{% endif %}` +
        `{% set tr = state_attr('${e}', 'trend') %}` +
        `{% if tr == 'rising' %} · ↑{% elif tr == 'falling' %} · ↓{% endif %}`
      : "") +
    `{% endif %}`
  );
}

function readingCard(room, ctx) {
  const table = BANDS[room.band];
  if (ctx.caps.has("mushroom-template-card")) {
    const card = {
      type: "custom:mushroom-template-card",
      entity: room.avg,
      primary: readingPrimary(room),
      secondary: readingSecondary(room, ctx.opts.detail),
      icon: room.icon,
      tap_action: { action: "more-info" },
      hold_action: { action: "more-info" },
      grid_options: { columns: "full" },
    };
    const color = iconColorTemplate(room.avg, table);
    const badge = badgeIconTemplate(room.avg, table);
    if (color) card.icon_color = color;
    if (badge) {
      card.badge_icon = badge;
      card.badge_color = "red";
    }
    return withGlass(card, ctx, { inner: GLASS_READING, tint: 58 });
  }
  return nativeReading(room, ctx);
}

/* Core-only reading: one tile per band, exactly one of which is ever visible,
 * so the colour still tracks the temperature without any JS deciding it. */
function nativeReading(room, ctx) {
  const cuts = bandCuts(BANDS[room.band]);
  const base = {
    type: "tile",
    entity: room.avg,
    name: room.area,
    icon: room.icon,
    state_content: ["state", "summary"],
    grid_options: { columns: "full" },
  };
  if (!cuts.length) {
    return withGlass({ ...base }, ctx, { tint: 58 });
  }
  const cards = cuts.map((cut) =>
    withGlass(
      {
        ...base,
        color: cut.color,
        visibility: [bandVisibility(room.avg, cut)],
      },
      ctx,
      { tint: 58 }
    )
  );
  cards.push(
    withGlass(
      {
        ...base,
        color: "grey",
        visibility: [{ condition: "state", entity: room.avg, state: OFFLINE }],
      },
      ctx,
      { tint: 58 }
    )
  );
  return cards;
}

/* ------------------------------------------------------------ the buttons */

/* Two per room. "Sensors" reveals every member; "Offline" reveals only the
 * dark ones. Both are plain toggles on switches the integration owns, so the
 * label can state the count live and the reveal survives a page load. */
function revealButton(room, ctx, kind) {
  const toggle = kind === "offline" ? ctx.toggleOffline : ctx.toggleSensors;
  if (!toggle) return null;
  const offlineCount =
    `{% set o = state_attr('${room.avg}', 'offline') %}` +
    `{% set mc = state_attr('${room.avg}', 'member_count') | int(0) %}` +
    `{% set rp = state_attr('${room.avg}', 'reporting') | int(0) %}` +
    `{% set n = (o | count) if o is not none else (mc - rp) %}`;
  const label =
    kind === "offline"
      ? offlineCount +
        `{% if is_state('${toggle}', 'on') %}Hide offline` +
        `{% else %}{{ n }} offline{% endif %}`
      : coverageExpr(room.avg) +
        `{% if is_state('${toggle}', 'on') %}Hide sensors` +
        `{% else %}{{ mc }} sensor{{ 's' if mc != 1 else '' }}{% endif %}`;
  const icon =
    kind === "offline"
      ? `{% if is_state('${toggle}', 'on') %}mdi:eye-off-outline{% else %}mdi:lan-disconnect{% endif %}`
      : `{% if is_state('${toggle}', 'on') %}mdi:eye-off-outline{% else %}mdi:format-list-bulleted{% endif %}`;
  const color =
    kind === "offline"
      ? `{% if is_state('${toggle}', 'on') %}grey{% else %}orange{% endif %}`
      : `{% if is_state('${toggle}', 'on') %}grey{% else %}blue{% endif %}`;

  // The offline button only exists while that room actually has a dark
  // member — evaluated live, never from a generate-time snapshot.
  const visibility =
    kind === "offline" && room.members.length
      ? [
          {
            condition: "or",
            conditions: room.members.map((m) => ({
              condition: "state",
              entity: m.id,
              state: OFFLINE,
            })),
          },
        ]
      : undefined;

  if (ctx.caps.has("mushroom-template-card")) {
    const card = {
      type: "custom:mushroom-template-card",
      entity: toggle,
      primary: label,
      icon,
      icon_color: color,
      tap_action: { action: "toggle" },
      grid_options: { columns: 6 },
    };
    if (visibility) card.visibility = visibility;
    return withGlass(card, ctx, {
      inner: GLASS_PILL_TEXT,
      radius: 999,
      tint: 46,
      lift: 0.6,
    });
  }
  const card = {
    type: "tile",
    entity: toggle,
    name: kind === "offline" ? "Offline" : "Sensors",
    icon: kind === "offline" ? "mdi:lan-disconnect" : "mdi:format-list-bulleted",
    hide_state: true,
    tap_action: { action: "toggle" },
    grid_options: { columns: 6 },
  };
  if (visibility) card.visibility = visibility;
  return withGlass(card, ctx, { radius: 999, tint: 46, lift: 0.6 });
}

/* ------------------------------------------------------------ member tiles */

/* A reporting member shows when the Sensors button is on; a dark one shows
 * when the Offline button is on. Both conditions are live, so a sensor that
 * dies while the page is open moves from one button to the other on its own. */
function memberVisibility(member, ctx) {
  const conditions = [];
  if (ctx.toggleSensors) {
    conditions.push({ condition: "state", entity: ctx.toggleSensors, state: "on" });
  }
  if (ctx.toggleOffline) {
    conditions.push({
      condition: "and",
      conditions: [
        { condition: "state", entity: ctx.toggleOffline, state: "on" },
        { condition: "state", entity: member.id, state: OFFLINE },
      ],
    });
  }
  if (!conditions.length) return undefined;
  return [{ condition: "or", conditions }];
}

function memberTile(member, room, ctx) {
  const mismatched = member.unit && room.unit && member.unit !== room.unit;
  const card = {
    type: "tile",
    entity: member.id,
    name: mismatched ? `${member.name} (${member.unit})` : member.name,
    state_content: ["state", "last_changed"],
    grid_options: { columns: "full" },
  };
  const visibility = memberVisibility(member, ctx);
  if (visibility) card.visibility = visibility;
  return withGlass(card, ctx, { radius: 16, tint: 40, lift: 0.5 });
}

function excludedTile(member, ctx) {
  const card = {
    type: "tile",
    entity: member.id,
    name: member.name,
    icon: "mdi:sigma-lower",
    color: "disabled",
    state_content: ["state", "last_changed"],
    grid_options: { columns: "full" },
  };
  if (ctx.toggleSensors) {
    card.visibility = [
      { condition: "state", entity: ctx.toggleSensors, state: "on" },
    ];
  }
  return withGlass(card, ctx, { radius: 16, tint: 40, lift: 0.5 });
}

/* ---------------------------------------------------------------- section */

/* One section per area: the name, the temperature, the two buttons, then the
 * sensors themselves waiting behind those buttons. */
function roomSection(room, ctx) {
  const cards = [
    {
      type: "heading",
      heading: room.area,
      heading_style: "title",
      icon: room.icon,
    },
  ];
  const reading = readingCard(room, ctx);
  cards.push(...(Array.isArray(reading) ? reading : [reading]));

  if (room.members.length) {
    const sensors = revealButton(room, ctx, "sensors");
    const offline = revealButton(room, ctx, "offline");
    if (sensors) cards.push(sensors);
    if (offline) cards.push(offline);
    for (const member of room.members) cards.push(memberTile(member, room, ctx));
  }
  for (const member of room.excluded) cards.push(excludedTile(member, ctx));
  return { type: "grid", cards };
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
  const outdoor = rooms.filter((r) => r.group === "outdoors").map((r) => r.avg);
  const card = {
    type: "markdown",
    content: statusTemplate(inside, outdoor),
    grid_options: { columns: "full" },
  };
  return {
    type: "grid",
    column_span: 3,
    cards: [withGlass(card, ctx, { tint: 52 })],
  };
}

/* ------------------------------------------------------------------ views */

function roomsView(deviceClass, rooms, ctx) {
  const meta = CLASS_META[deviceClass] || {
    title: deviceClass,
    path: deviceClass.replace(/[^a-z0-9]+/g, "-"),
    icon: "mdi:chart-box-outline",
  };
  const sections = [statusSection(rooms, ctx)].concat(
    rooms.map((room) => roomSection(room, ctx))
  );
  return {
    title: meta.title,
    path: meta.path,
    icon: meta.icon,
    type: "sections",
    max_columns: 3,
    sections,
  };
}

function trendsView(rooms, ctx) {
  const sections = [
    {
      type: "grid",
      column_span: 3,
      cards: [
        {
          type: "heading",
          heading: `All rooms · hourly mean · ${ctx.opts.statsDays} days`,
          heading_style: "title",
          icon: "mdi:chart-line",
        },
        withGlass(
          {
            type: "statistics-graph",
            entities: rooms.map((room) => room.avg),
            stat_types: ["mean"],
            period: "hour",
            days_to_show: ctx.opts.statsDays,
            chart_type: "line",
            grid_options: { columns: "full" },
          },
          ctx,
          { tint: 52 }
        ),
      ],
    },
  ];
  for (const room of rooms) {
    sections.push({
      type: "grid",
      cards: [
        {
          type: "heading",
          heading: room.area,
          heading_style: "subtitle",
          icon: room.icon,
        },
        withGlass(
          {
            type: "statistics-graph",
            entities: [room.avg],
            stat_types: ["min", "mean", "max"],
            period: "hour",
            days_to_show: ctx.opts.statsDays,
            chart_type: "line",
            hide_legend: true,
            grid_options: { columns: "full" },
          },
          ctx,
          { tint: 48 }
        ),
      ],
    });
  }
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
    const caps = await capabilities(opts);
    const ctx = {
      opts,
      caps,
      toggleSensors: resolveToggle(hass, SENSOR_TOGGLES),
      toggleOffline: resolveToggle(hass, OFFLINE_TOGGLES),
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
