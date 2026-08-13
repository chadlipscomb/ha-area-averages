/* Exercise the dashboard strategy against a live-derived fixture, with no
 * browser and no login. Refresh the fixture with tools/make_fixture.py.
 *
 *   node tools/test_strategy.mjs            # assert + summary
 *   node tools/test_strategy.mjs --json     # also dump the generated config
 *
 * The suite runs the whole assertion set four times: with and without the
 * custom cards defined, and with and without the contract attributes the
 * integration publishes (offline / summary / healthy / minimum / maximum /
 * spread / trend / trend_rate). The "legacy" runs use the untouched fixture,
 * which is what a browser sees when the JS ships ahead of the Python.
 *
 * Exit code 1 if any assertion fails.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");
const STRATEGY = path.join(REPO, "www", "area_averages_strategy.js");
const FIXTURE = path.join(HERE, "fixture.json");
const INPUT_BOOLEAN = "input_boolean.show_offline_sensors";
const SWITCH = "switch.area_averages_show_offline";
const OFFLINE = ["unavailable", "unknown"];
const CUSTOM_CARDS = [
  "mushroom-template-card",
  "mini-graph-card",
  "stack-in-card",
];
const CONTRACT = [
  "offline",
  "summary",
  "healthy",
  "minimum",
  "maximum",
  "spread",
  "trend",
  "trend_rate",
];
const BAND_COLORS = ["blue", "light-blue", "green", "amber", "red"];
const EQUIPMENT = ["3d_printer", "bench", "fridge"];
const OUTDOORS = ["outside", "weather"];

const failures = [];
const checks = [];
function check(name, condition, detail = "") {
  checks.push(name);
  if (!condition) failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

const copy = (value) => JSON.parse(JSON.stringify(value));
const round2 = (value) => Math.round(value * 100) / 100;

/* ------------------------------------------------------------ strategy IO */

/* customElements shim: define/get/whenDefined driven by a settable set.
 * whenDefined resolves synchronously for defined names and stays pending for
 * the rest, so the strategy's probe timeout is genuinely exercised. */
function loadStrategy(defined) {
  const code = fs.readFileSync(STRATEGY, "utf8");
  const registry = {};
  const known = new Set(defined);
  const shim = {
    define: (name, cls) => (registry[name] = cls),
    get: () => undefined,
    whenDefined: (name) =>
      known.has(name) ? Promise.resolve(name) : new Promise(() => {}),
  };
  const quiet = { info() {}, warn() {}, error() {}, log() {} };
  new Function("customElements", "console", code)(shim, quiet);
  const names = Object.keys(registry);
  check("registers exactly one custom element", names.length === 1, names.join(","));
  check(
    "element name is a dashboard strategy",
    names[0] === "ll-strategy-dashboard-area-temperatures",
    names[0]
  );
  return registry[names[0]];
}

function walkCards(cards, fn) {
  for (const card of cards || []) {
    fn(card);
    if (Array.isArray(card.cards)) walkCards(card.cards, fn);
  }
}

function allCards(view) {
  const found = [];
  for (const section of view.sections || []) walkCards(section.cards, (c) => found.push(c));
  walkCards(view.cards, (c) => found.push(c));
  return found;
}

function allBadges(view) {
  const badges = [...(view.badges || [])];
  for (const card of allCards(view)) badges.push(...(card.badges || []));
  return badges;
}

function entityIdsOf(card) {
  const ids = [];
  if (card.entity) ids.push(card.entity);
  for (const entity of card.entities || []) {
    ids.push(typeof entity === "string" ? entity : entity.entity);
  }
  return ids;
}

const isTile = (card) =>
  card.type === "tile" || card.type === "custom:mushroom-template-card";

/* -------------------------------------------------------------- the model */

function averageIds(hass) {
  return Object.values(hass.entities || {})
    .filter((e) => e.platform === "area_averages")
    .filter((e) => hass.states[e.entity_id]?.attributes?.device_class === "temperature")
    .map((e) => e.entity_id)
    .sort();
}

function areaIdOf(hass, avg) {
  return hass.entities[avg].area_id;
}

function membersOf(hass, avg) {
  return hass.states[avg].attributes.sensors || [];
}

function excludedOf(hass, avg) {
  return hass.states[avg].attributes.excluded || [];
}

function isOffline(hass, entityId) {
  const state = hass.states[entityId];
  if (!state || OFFLINE.includes(state.state)) return true;
  return !Number.isFinite(parseFloat(state.state));
}

function summaryText(memberCount, reporting) {
  if (reporting === 0) return "no sensors reporting";
  if (memberCount === 1 && reporting === 1) return "1 sensor";
  if (reporting < memberCount) return `${reporting} of ${memberCount} sensors`;
  return `${memberCount} sensors`;
}

/* What the backend publishes once BE-2 lands, computed the same way, so the
 * new rendering paths can be tested before the Python is deployed. Only fills
 * what the fixture is missing — a real value always wins. */
function enrich(hass) {
  const out = copy(hass);
  averageIds(out).forEach((avg, index) => {
    const attributes = out.states[avg].attributes;
    const fill = (name, value) => {
      if (attributes[name] === undefined) attributes[name] = value;
    };
    const unit = attributes.unit_of_measurement;
    const members = attributes.sensors || [];
    const offline = [];
    const values = [];
    for (const id of members) {
      const state = out.states[id];
      const raw = state ? parseFloat(state.state) : NaN;
      if (!state || OFFLINE.includes(state.state) || !Number.isFinite(raw)) {
        offline.push(id);
        continue;
      }
      const memberUnit = state.attributes.unit_of_measurement;
      let value = raw;
      if (memberUnit === "°C" && unit === "°F") value = raw * 1.8 + 32;
      if (memberUnit === "°F" && unit === "°C") value = (raw - 32) / 1.8;
      values.push(round2(value));
    }
    fill("offline", offline.sort());
    fill("member_count", members.length);
    fill("reporting", values.length);
    fill("summary", summaryText(members.length, values.length));
    fill("healthy", values.length > 0 && (attributes.issues || []).length === 0);
    fill("minimum", values.length ? Math.min(...values) : null);
    fill("maximum", values.length ? Math.max(...values) : null);
    fill(
      "spread",
      values.length > 1 ? round2(Math.max(...values) - Math.min(...values)) : null
    );
    fill("trend", ["rising", "falling", "steady", "unknown"][index % 4]);
    fill("trend_rate", attributes.trend === "unknown" ? null : 1.25);
  });
  return out;
}

function stripContract(hass) {
  const out = copy(hass);
  for (const avg of averageIds(out)) {
    for (const name of CONTRACT) delete out.states[avg].attributes[name];
  }
  return out;
}

/* --------------------------------------------------------- band arithmetic */

/* The band variants a native hero emits for one average, as evaluable windows. */
function bandWindows(view, avg) {
  return allCards(view)
    .filter((c) => c.type === "tile" && c.entity === avg && c.color)
    .filter((c) => (c.visibility || []).some((v) => v.condition === "numeric_state"))
    .map((c) => {
      const condition = c.visibility.find((v) => v.condition === "numeric_state");
      return {
        color: c.color,
        above: condition.above === undefined ? null : condition.above,
        below: condition.below === undefined ? null : condition.below,
      };
    });
}

function matching(windows, value) {
  return windows.filter(
    (w) =>
      (w.above === null || value > w.above) && (w.below === null || value < w.below)
  );
}

/* ------------------------------------------------------------- assertions */

function assertAll(label, config, hass, mode) {
  const say = (name) => `[${label}] ${name}`;
  const json = JSON.stringify(config);
  const avgIds = averageIds(hass);

  check(say("returns views"), Array.isArray(config.views) && config.views.length > 0);
  check(
    say("survives JSON round-trip"),
    JSON.stringify(JSON.parse(json)) === json
  );

  if (!mode.custom) {
    check(
      say("emits no custom card types"),
      !json.includes('"custom:'),
      (json.match(/"custom:[a-z-]+/g) || []).slice(0, 3).join(",")
    );
  }

  const paths = config.views.map((v) => v.path);
  check(say("view paths are unique"), new Set(paths).size === paths.length);

  for (const view of config.views) {
    check(say(`view "${view.title}" has a title`), typeof view.title === "string" && view.title);
    check(say(`view "${view.title}" has a path`), typeof view.path === "string" && view.path);
    const cards = allCards(view);
    check(say(`view "${view.title}" has cards`), cards.length > 0);
    for (const card of cards) {
      check(
        say(`card in "${view.title}" has a type`),
        typeof card.type === "string",
        JSON.stringify(card).slice(0, 80)
      );
      for (const id of entityIdsOf(card)) {
        check(say(`entity ${id} exists in fixture`), Boolean(hass.states[id]), view.title);
      }
    }
    for (const badge of allBadges(view)) {
      check(say(`badge type is "entity" (${badge.entity})`), badge.type === "entity");
      check(say(`badge entity ${badge.entity} exists`), Boolean(hass.states[badge.entity]));
    }
  }

  const rooms = config.views.find((v) => v.path === "rooms");
  const trends = config.views.find((v) => v.path === "trends");
  const subviews = config.views.filter((v) => v.subview);
  check(say("Rooms view exists"), Boolean(rooms));
  check(say("Trends view exists"), Boolean(trends));
  check(say("one subview per average"), subviews.length === avgIds.length, `${subviews.length}`);

  /* --- FE-3: the Rooms view is heroes only -------------------------------- */
  const memberish = new Set();
  for (const avg of avgIds) {
    for (const id of membersOf(hass, avg)) memberish.add(id);
    for (const id of excludedOf(hass, avg)) memberish.add(id);
  }
  const roomCards = allCards(rooms);
  for (const card of roomCards.filter(isTile)) {
    check(
      say(`Rooms view carries no member tile (${card.entity})`),
      !memberish.has(card.entity)
    );
    check(
      say(`Rooms view carries no toggle tile (${card.entity})`),
      card.entity !== INPUT_BOOLEAN && card.entity !== SWITCH
    );
  }

  const statusSection = (rooms.sections || [])[0];
  check(say("Status section is first"), statusSection.cards[0].heading === "Status");
  check(say("Status section spans 3 columns"), statusSection.column_span === 3);
  const markdown = statusSection.cards.find((c) => c.type === "markdown");
  check(say("Status card is markdown"), Boolean(markdown));
  check(say("Status drops the healthy paragraph"), !markdown.content.includes("All sensors healthy"));
  check(say("Status drops the tick emoji"), !markdown.content.includes("✅"));
  check(say("Status uses a GitHub alert"), markdown.content.includes("[!{{ 'CAUTION'"));
  check(say("Status reads the offline attribute"), markdown.content.includes("'offline'"));
  check(
    say("Status falls back when offline is absent"),
    markdown.content.includes("(mc - rp)")
  );

  /* --- FE-3: groups, order and membership --------------------------------- */
  const groupSections = (rooms.sections || []).slice(1);
  const groupTitles = groupSections.map((s) => s.cards[0].heading);
  check(
    say("group order is Inside, Outdoors, Equipment & Cold"),
    JSON.stringify(groupTitles) === JSON.stringify(["Inside", "Outdoors", "Equipment & Cold"]),
    groupTitles.join("|")
  );
  const sectionOf = {};
  groupSections.forEach((section) => {
    walkCards(section.cards, (card) => {
      for (const id of entityIdsOf(card)) {
        if (avgIds.includes(id)) sectionOf[id] = section.cards[0].heading;
      }
    });
  });
  for (const avg of avgIds) {
    const areaId = areaIdOf(hass, avg);
    const expected = EQUIPMENT.includes(areaId)
      ? "Equipment & Cold"
      : OUTDOORS.includes(areaId)
        ? "Outdoors"
        : "Inside";
    check(say(`${areaId} sits in ${expected}`), sectionOf[avg] === expected, sectionOf[avg]);
  }

  /* --- FE-2: exactly one hero per average --------------------------------- */
  for (const avg of avgIds) {
    const heroes = roomCards.filter((c) => isTile(c) && c.entity === avg);
    const areaId = areaIdOf(hass, avg);
    const banded = !EQUIPMENT.filter((a) => a !== "fridge").includes(areaId) &&
      !OUTDOORS.includes(areaId);
    const expected = mode.custom ? 1 : banded ? (areaId === "fridge" ? 4 : 6) : 1;
    check(
      say(`average ${avg} appears ${expected}x as a hero`),
      heroes.length === expected,
      `found ${heroes.length}`
    );
    for (const hero of heroes) {
      check(say(`hero ${avg} has an icon`), Boolean(hero.icon));
      check(
        say(`hero ${avg} navigates to its subview`),
        hero.tap_action?.action === "navigate"
      );
      check(say(`hero ${avg} holds for more-info`), hero.hold_action?.action === "more-info");
    }
  }

  /* --- FE-2: colour comes from the band table, nothing else ---------------- */
  for (const card of roomCards) {
    if (!card.color) continue;
    check(
      say(`hero colour ${card.color} comes from the band table`),
      [...BAND_COLORS, "grey", "disabled"].includes(card.color)
    );
  }
  for (const avg of avgIds) {
    const areaId = areaIdOf(hass, avg);
    const cards = roomCards.filter((c) => entityIdsOf(c).includes(avg));
    const flat = JSON.stringify(cards);
    if (["3d_printer", "bench", "outside", "weather"].includes(areaId)) {
      check(say(`${areaId} carries no comfort colour`), !flat.includes('"color"'));
      check(say(`${areaId} carries no icon_color`), !flat.includes("icon_color"));
      check(say(`${areaId} carries no fire badge`), !flat.includes("mdi:fire"));
      check(say(`${areaId} carries no colour thresholds`), !flat.includes("color_thresholds"));
    }
  }

  if (mode.custom) {
    for (const avg of avgIds) {
      const areaId = areaIdOf(hass, avg);
      const stack = roomCards.find(
        (c) => c.type === "custom:stack-in-card" && c.cards[0].entity === avg
      );
      check(say(`${areaId} hero is a stack-in-card`), Boolean(stack));
      const hero = stack.cards[0];
      const graph = stack.cards[1];
      check(say(`${areaId} hero is a mushroom template card`), hero.type === "custom:mushroom-template-card");
      check(say(`${areaId} hero names the area`), typeof hero.primary === "string" && hero.primary.length > 0);
      check(say(`${areaId} hero secondary is a template`), hero.secondary.includes("{%"));
      check(say(`${areaId} hero secondary rebuilds summary when absent`), hero.secondary.includes("sensors'"));
      check(say(`${areaId} graph is a mini-graph-card`), graph.type === "custom:mini-graph-card");
      check(say(`${areaId} graph hides its chrome`), graph.show.name === false && graph.show.state === false);

      const thresholds = (graph.color_thresholds || []).map((t) => t.value);
      if (areaId === "fridge") {
        check(
          say("fridge uses the fridge ladder"),
          JSON.stringify(thresholds) === JSON.stringify([0, 45, 50]),
          thresholds.join(",")
        );
        check(say("fridge badge is not fire"), hero.badge_icon.includes("mdi:alert-circle"));
        check(say("fridge badge trips above 50"), hero.badge_icon.includes("> 50"));
        check(say("fridge ladder has no 68"), !hero.icon_color.includes("< 68"));
      } else if (!["3d_printer", "bench", "outside", "weather"].includes(areaId)) {
        check(
          say(`${areaId} uses the comfort ladder`),
          JSON.stringify(thresholds) === JSON.stringify([0, 65, 68, 78, 84]),
          thresholds.join(",")
        );
        for (const fragment of ["< 65", "< 68", "< 78", "<= 84", "disabled"]) {
          check(
            say(`${areaId} icon_color ladder contains "${fragment}"`),
            hero.icon_color.includes(fragment)
          );
        }
        check(say(`${areaId} badge is mdi:fire above 84`), hero.badge_icon.includes("mdi:fire") && hero.badge_icon.includes("> 84"));
        check(say(`${areaId} badge is red`), hero.badge_color === "red");
      } else {
        check(say(`${areaId} has no thresholds`), graph.color_thresholds === undefined);
      }
    }
  } else {
    const comfort = avgIds.find((a) => areaIdOf(hass, a) === "bedroom");
    const fridge = avgIds.find((a) => areaIdOf(hass, a) === "fridge");
    const comfortWindows = bandWindows(rooms, comfort);
    const fridgeWindows = bandWindows(rooms, fridge);
    check(say("comfort ladder emits 5 bands"), comfortWindows.length === 5, `${comfortWindows.length}`);
    check(say("fridge ladder emits 3 bands"), fridgeWindows.length === 3, `${fridgeWindows.length}`);
    const expectComfort = {
      60: "blue", 64.99: "blue", 65: "light-blue", 67.99: "light-blue",
      68: "green", 77.99: "green", 78: "amber", 84: "amber",
      84.01: "red", 90: "red",
    };
    for (const [value, color] of Object.entries(expectComfort)) {
      const hits = matching(comfortWindows, Number(value));
      check(
        say(`comfort band for ${value} is exactly ${color}`),
        hits.length === 1 && hits[0].color === color,
        hits.map((h) => h.color).join(",") || "none"
      );
    }
    const expectFridge = {
      40: "light-blue", 44.99: "light-blue", 45: "amber",
      50: "amber", 50.01: "red", 60: "red",
    };
    for (const [value, color] of Object.entries(expectFridge)) {
      const hits = matching(fridgeWindows, Number(value));
      check(
        say(`fridge band for ${value} is exactly ${color}`),
        hits.length === 1 && hits[0].color === color,
        hits.map((h) => h.color).join(",") || "none"
      );
    }
    for (const avg of avgIds) {
      const grey = roomCards.filter(
        (c) =>
          c.entity === avg &&
          (c.visibility || []).some(
            (v) => v.condition === "state" && JSON.stringify(v.state) === JSON.stringify(OFFLINE)
          )
      );
      const areaId = areaIdOf(hass, avg);
      const banded = !["3d_printer", "bench", "outside", "weather"].includes(areaId);
      check(
        say(`${areaId} keeps a hero while unavailable`),
        banded ? grey.length === 1 : grey.length === 0
      );
    }
    check(
      say("native heroes carry no history feature"),
      !JSON.stringify(rooms).includes("trend-graph")
    );
  }

  /* --- FE-3: subviews own the members ------------------------------------- */
  const subviewPaths = new Set(subviews.map((v) => v.path));
  for (const avg of avgIds) {
    const areaId = areaIdOf(hass, avg);
    const view = subviews.find((v) => v.path === `room-${areaId}`);
    check(say(`subview room-${areaId} exists`), Boolean(view));
    if (!view) continue;
    check(say(`subview room-${areaId} is a subview`), view.subview === true);
    check(say(`subview room-${areaId} has an icon`), Boolean(view.icon));
    const cards = allCards(view);
    check(
      say(`subview room-${areaId} shows the hero`),
      cards.some((c) => entityIdsOf(c).includes(avg))
    );
    check(
      say(`subview room-${areaId} graphs history`),
      cards.some((c) => c.type === "history-graph")
    );
    for (const id of excludedOf(hass, avg)) {
      const tile = cards.find((c) => isTile(c) && c.entity === id);
      check(say(`excluded ${id} is on its own subview`), Boolean(tile));
      if (tile) {
        check(say(`excluded ${id} is vertical`), tile.vertical === true);
        check(
          say(`excluded ${id} shows state only`),
          JSON.stringify(tile.state_content) === JSON.stringify(["state"])
        );
      }
    }
    if (excludedOf(hass, avg).length) {
      const heading = cards.find(
        (c) => c.type === "heading" && String(c.heading).startsWith("Not in average")
      );
      check(say(`subview room-${areaId} labels its exclusions`), Boolean(heading));
    }
    const names = cards
      .filter((c) => isTile(c) && membersOf(hass, avg).includes(c.entity))
      .map((c) => c.name || c.primary);
    check(
      say(`subview room-${areaId} names every member uniquely`),
      new Set(names).size === names.length,
      names.join("|")
    );
    for (const name of names) {
      check(say(`member name in room-${areaId} is not empty`), Boolean(name && String(name).trim()));
    }
  }

  const tileCount = {};
  for (const view of subviews) {
    for (const card of allCards(view).filter(isTile)) {
      if (!memberish.has(card.entity)) continue;
      tileCount[card.entity] = (tileCount[card.entity] || 0) + 1;
    }
  }
  for (const id of memberish) {
    check(say(`${id} appears exactly once across subviews`), tileCount[id] === 1, `${tileCount[id]}`);
  }

  /* --- FE-3: every navigation lands somewhere real ------------------------ */
  const navPaths = new Set();
  for (const card of roomCards) {
    if (card.tap_action?.action === "navigate") navPaths.add(card.tap_action.navigation_path);
  }
  for (const badge of allBadges(rooms)) {
    if (badge.tap_action?.action === "navigate") navPaths.add(badge.tap_action.navigation_path);
  }
  for (const navPath of navPaths) {
    const last = navPath.split("/").pop();
    check(
      say(`navigation ${navPath} resolves`),
      subviewPaths.has(last) || last === "trends" || last === "rooms",
      navPath
    );
  }

  /* --- FE-6: Trends is statistics only ------------------------------------ */
  const trendCards = allCards(trends);
  check(
    say("Trends carries no history-graph"),
    trendCards.every((c) => c.type !== "history-graph")
  );
  check(say("Trends overview spans 3 columns"), trends.sections[0].column_span === 3);
  const overview = trends.sections[0].cards.find((c) => c.type === "statistics-graph");
  check(say("Trends overview lists every average"), overview.entities.length === avgIds.length);
  for (const avg of avgIds) {
    check(say(`Trends overview includes ${avg}`), overview.entities.includes(avg));
    const solo = trendCards.filter(
      (c) => c.type === "statistics-graph" && c.entities.length === 1 && c.entities[0] === avg
    );
    check(say(`Trends charts ${avg} once on its own`), solo.length === 1, `${solo.length}`);
    if (solo[0]) {
      check(
        say(`Trends ${avg} shows min/mean/max`),
        JSON.stringify(solo[0].stat_types) === JSON.stringify(["min", "mean", "max"])
      );
      check(say(`Trends ${avg} uses hourly statistics`), solo[0].period === "hour");
    }
  }
  for (const card of trendCards) {
    for (const id of entityIdsOf(card)) {
      check(say(`Trends entity ${id} is an average`), !memberish.has(id));
    }
  }

  return { rooms, trends, subviews };
}

/* --------------------------------------------------------- focused checks */

function toggleChecks(Strategy, base) {
  const run = async (hass, config = {}) => Strategy.generate(config, hass);

  /* Build every toggle scenario explicitly. The fixture is generated from a
   * live HA, which owns switch.area_averages_show_offline once the
   * integration is deployed — so never infer a scenario from what the base
   * fixture happens to contain, always assert the starting point. */
  const scenario = ({ legacy = false, modern = false }) => {
    const out = copy(base);
    delete out.states[SWITCH];
    delete out.states[INPUT_BOOLEAN];
    const stub = (id) => ({
      entity_id: id,
      state: "off",
      attributes: { friendly_name: "Show offline sensors" },
    });
    if (legacy) out.states[INPUT_BOOLEAN] = stub(INPUT_BOOLEAN);
    if (modern) out.states[SWITCH] = stub(SWITCH);
    return out;
  };

  return (async () => {
    /* only the hand-made helper exists (the pre-BE-7 system) */
    const legacyHass = scenario({ legacy: true });
    const legacy = await run(legacyHass);
    const legacyJson = JSON.stringify(legacy);
    check("toggle: input_boolean is used when it is all there is", legacyJson.includes(INPUT_BOOLEAN));
    check("toggle: no switch is invented", !legacyJson.includes(SWITCH));

    /* the integration-owned switch wins (today's live system) */
    const withSwitch = scenario({ legacy: true, modern: true });
    const preferred = await run(withSwitch);
    const preferredJson = JSON.stringify(preferred);
    check("toggle: switch wins over input_boolean", preferredJson.includes(SWITCH));
    check("toggle: input_boolean is dropped once the switch exists", !preferredJson.includes(INPUT_BOOLEAN));

    /* neither exists — the reveal machinery must vanish, not break */
    const bare = scenario({});
    const none = await run(bare);
    const noneJson = JSON.stringify(none);
    check("toggle: nothing references a missing toggle", !noneJson.includes(INPUT_BOOLEAN) && !noneJson.includes(SWITCH));
    check("toggle: generate() still returns views", none.views.length > 0);
    let conditional = 0;
    let memberTiles = 0;
    for (const view of none.views) {
      for (const card of allCards(view)) {
        if (!isTile(card)) continue;
        const isMember = Object.values(bare.entities)
          .filter((e) => e.platform === "area_averages")
          .some((e) =>
            (bare.states[e.entity_id]?.attributes?.sensors || []).includes(
              card.entity
            )
          );
        if (!isMember) continue;
        memberTiles += 1;
        if (card.visibility) conditional += 1;
      }
      for (const badge of allBadges(view)) {
        check(
          "toggle: no badge points at a missing toggle",
          badge.entity !== INPUT_BOOLEAN && badge.entity !== SWITCH
        );
      }
    }
    check("toggle: member tiles exist to check", memberTiles > 0, `${memberTiles}`);
    check("toggle: every member tile is unconditional", conditional === 0, `${conditional} conditional`);

    /* with a toggle, every offline room gets exactly one reveal tile.
     * Asserted against the switch, which is what the live system resolves. */
    const revealed = await run(withSwitch);
    for (const view of revealed.views.filter((v) => v.subview)) {
      const cards = allCards(view);
      const reveal = cards.filter((c) => c.entity === SWITCH);
      const avg = averageIds(base).find((a) => `room-${areaIdOf(base, a)}` === view.path);
      const members = membersOf(base, avg);
      // The reveal control must exist for EVERY room with members, whether or
      // not anything is offline at generate time — generate() runs once per
      // page load, so a room that goes dark later would otherwise hide its
      // members with no way to bring them back.
      check(
        `toggle: ${view.path} has a reveal tile regardless of current state`,
        reveal.length === (members.length ? 1 : 0),
        `${reveal.length} for ${members.length} members`
      );
      // ...and its visibility must be an OR over that room's own members, so
      // it surfaces exactly when one of them is actually dark.
      for (const tile of reveal) {
        const covered = JSON.stringify(tile.visibility);
        for (const id of members) {
          check(`toggle: ${view.path} reveal covers ${id}`, covered.includes(id));
        }
      }
      for (const tile of reveal) {
        check(`toggle: ${view.path} reveal tile toggles`, tile.tap_action?.action === "toggle");
        check(`toggle: ${view.path} reveal tile is conditional`, Array.isArray(tile.visibility));
      }
      const badges = allBadges(view);
      // Same rule as the reveal tile: the badge pair is emitted for every room
      // with members and gates itself live, so a room that dies after page load
      // still offers a way back.
      check(
        `toggle: ${view.path} emits the reveal badge pair when it has members`,
        badges.length === (members.length ? 2 : 0),
        `${badges.length} for ${members.length} members`
      );
      for (const badge of badges) {
        const covered = JSON.stringify(badge.visibility);
        for (const id of members) {
          check(`toggle: ${view.path} badge covers ${id}`, covered.includes(id));
        }
      }
      for (const badge of badges) {
        check(`toggle: ${view.path} badge is an entity badge`, badge.type === "entity");
        check(`toggle: ${view.path} badge toggles`, badge.tap_action?.action === "toggle");
      }
      /* member tiles keep the hide-when-offline contract */
      for (const card of cards.filter((c) => isTile(c) && membersOf(base, avg).includes(c.entity))) {
        const flat = JSON.stringify(card.visibility);
        check(
          `toggle: ${card.entity} hides when offline`,
          flat.includes(card.entity) && flat.includes(SWITCH)
        );
      }
    }
  })();
}

async function badgeChecks(Strategy, base) {
  const config = await Strategy.generate({}, base);
  const rooms = config.views.find((v) => v.path === "rooms");
  const badges = rooms.badges || [];
  check("badges: Rooms view carries badges", badges.length > 0);
  for (const badge of badges) {
    check(`badges: ${badge.entity} exists in the fixture`, Boolean(base.states[badge.entity]));
    check(`badges: ${badge.entity} shows its name`, badge.show_name === true);
  }
  const outdoor = badges.filter((b) => !b.icon.includes("lan-disconnect"));
  check("badges: one per outdoor average", outdoor.length === 2, `${outdoor.length}`);
  const offlineBadges = badges.filter((b) => b.icon === "mdi:lan-disconnect");
  check("badges: an offline badge exists while sensors are dark", offlineBadges.length > 0);
  check("badges: offline badges are capped", offlineBadges.length <= 3, `${offlineBadges.length}`);
  // Each offline badge must name the room it points at, and gate itself on that
  // room's OWN members only. A badge whose visibility spans other rooms' sensors
  // would keep naming the room chosen at generate time while a different room is
  // the one actually dark.
  for (const badge of offlineBadges) {
    const or = badge.visibility[0];
    check(
      `badges: ${badge.name} offline badge is an OR over members`,
      or.condition === "or" && or.conditions.length > 0
    );
    const members = new Set(base.states[badge.entity]?.attributes?.sensors || []);
    for (const condition of or.conditions) {
      check(
        `badges: ${badge.name} badge only watches its own members (${condition.entity})`,
        members.has(condition.entity),
        `not a member of ${badge.entity}`
      );
    }
    check(
      `badges: ${badge.name} badge watches a currently-dark member`,
      or.conditions.some((c) => isOffline(base, c.entity)),
      "badge would be emitted for a healthy room"
    );
  }
  const offlineBadge = offlineBadges[0];
  const or = offlineBadge.visibility[0];
  for (const condition of or.conditions.filter((c) => isOffline(base, c.entity))) {
    check(
      `badges: ${condition.entity} really is offline`,
      isOffline(base, condition.entity)
    );
  }
  check("badges: offline badge is red", offlineBadge.color === "red");

  /* a missing entity must remove its badge, not break the page */
  const trimmed = copy(base);
  const weather = averageIds(trimmed).find((a) => areaIdOf(trimmed, a) === "weather");
  delete trimmed.states[weather];
  delete trimmed.entities[weather];
  const without = await Strategy.generate({}, trimmed);
  const stillThere = JSON.stringify(without).includes(weather);
  check("badges: deleting the Weather average removes its badge", !stillThere);
  check("badges: generate() survives a missing average", without.views.length > 0);
  check(
    "badges: the Weather subview disappears too",
    !without.views.some((v) => v.path === "room-weather")
  );
}

async function optionChecks(Strategy, base) {
  /* grouping is an option, and the Status template follows it */
  const moved = await Strategy.generate(
    { areas: { bedroom: { group: "outdoors", band: "none" } } },
    base
  );
  const rooms = moved.views.find((v) => v.path === "rooms");
  const outdoors = rooms.sections.find((s) => s.cards[0].heading === "Outdoors");
  const bedroom = averageIds(base).find((a) => areaIdOf(base, a) === "bedroom");
  check(
    "options: areas override moves Bedroom outdoors",
    JSON.stringify(outdoors).includes(bedroom)
  );
  const markdown = rooms.sections[0].cards.find((c) => c.type === "markdown");
  check(
    "options: the Status inside-list follows the areas option",
    !markdown.content.includes('"bedroom"'),
    "bedroom should have left the inside list"
  );
  check(
    "options: the Status inside-list still holds the other rooms",
    markdown.content.includes('"kitchen"') && markdown.content.includes('"living_room"')
  );
  check(
    "options: Bedroom loses its comfort colour when regrouped",
    !JSON.stringify(outdoors.cards.filter((c) => JSON.stringify(c).includes(bedroom))).includes("icon_color")
  );

  /* history_hours and stats_days are honoured */
  const custom = await Strategy.generate({ history_hours: 6, stats_days: 30 }, base);
  const graphs = custom.views
    .flatMap((v) => allCards(v))
    .filter((c) => c.type === "history-graph" || c.type === "custom:mini-graph-card");
  check("options: history_hours reaches every graph", graphs.every((g) => g.hours_to_show === 6));
  const stats = custom.views
    .flatMap((v) => allCards(v))
    .filter((c) => c.type === "statistics-graph");
  check("options: stats_days reaches every statistics graph", stats.every((s) => s.days_to_show === 30));

  /* dashboard_path drives every navigation target */
  const renamed = await Strategy.generate({ dashboard_path: "elsewhere" }, base);
  const navs = renamed.views
    .flatMap((v) => allCards(v))
    .filter((c) => c.tap_action?.action === "navigate")
    .map((c) => c.tap_action.navigation_path);
  check("options: dashboard_path is honoured", navs.length > 0 && navs.every((p) => p.startsWith("/elsewhere/")));

  /* cards: "native" forces the fallback even when the elements are defined */
  const forced = await Strategy.generate({ cards: "native" }, base);
  check("options: cards:native forces core cards", !JSON.stringify(forced).includes('"custom:'));
}

async function nameChecks(Strategy, base) {
  const config = await Strategy.generate({ cards: "mushroom" }, base);
  const nameOf = (viewPath, entityId) => {
    const view = config.views.find((v) => v.path === viewPath);
    const card = allCards(view).find((c) => isTile(c) && c.entity === entityId);
    return card ? card.name || card.primary : null;
  };
  check(
    'names: "Bedroom ITH-11-B 0440 Temperature" becomes "ITH-11-B 0440"',
    nameOf("room-bedroom", "sensor.bedroom_ith_11_b_0440_temperature") === "ITH-11-B 0440",
    String(nameOf("room-bedroom", "sensor.bedroom_ith_11_b_0440_temperature"))
  );
  const generic = nameOf("room-office", "sensor.office_temp");
  check(
    'names: a member called only "Temperature" keeps a name',
    Boolean(generic) && generic.trim().length > 0,
    String(generic)
  );
  check(
    "names: the trailing device id survives",
    nameOf("room-bench", "sensor.tp359s_5848_temperature") === "TP359S (5848)",
    String(nameOf("room-bench", "sensor.tp359s_5848_temperature"))
  );
  check(
    "names: a leading area name is stripped",
    nameOf("room-fridge", "sensor.fridge_probe_temperature") === "Probe",
    String(nameOf("room-fridge", "sensor.fridge_probe_temperature"))
  );

  /* °C members inside a °F average declare themselves */
  const printer = config.views.find((v) => v.path === "room-3d_printer");
  const converted = allCards(printer).filter(
    (c) => c.type === "custom:mushroom-template-card" && c.entity.startsWith("sensor.altai")
  );
  check("units: every °C member is converted in mushroom mode", converted.length === 3, `${converted.length}`);
  for (const card of converted) {
    check(`units: ${card.entity} converts in Jinja`, card.secondary.includes("* 1.8"));
    check(`units: ${card.entity} shows the native reading too`, card.secondary.includes("°C)"));
    check(`units: ${card.entity} states the target unit`, card.secondary.includes("}} °F "));
    check(
      `units: ${card.entity} has no precomputed number`,
      !/\d\d\.\d ° F?/.test(card.secondary.replace(/\{\{[^}]*\}\}/g, ""))
    );
  }

  const native = await Strategy.generate({ cards: "native" }, base);
  const nativePrinter = native.views.find((v) => v.path === "room-3d_printer");
  const declared = allCards(nativePrinter).filter(
    (c) => c.type === "tile" && c.entity.startsWith("sensor.altai") && String(c.name).includes("(°C)")
  );
  check("units: native mode declares the mismatch in the name", declared.length === 3, `${declared.length}`);
}

/* Every partial mix of installed cards must still produce a whole dashboard. */
async function capabilityChecks(hass) {
  const shapes = {};
  const combos = [
    { label: "all", defined: CUSTOM_CARDS },
    { label: "no stack-in-card", defined: ["mushroom-template-card", "mini-graph-card"] },
    { label: "no mini-graph", defined: ["mushroom-template-card", "stack-in-card"] },
    { label: "mushroom only", defined: ["mushroom-template-card"] },
    { label: "graph only", defined: ["mini-graph-card"] },
    { label: "none", defined: [] },
  ];
  for (const combo of combos) {
    const Strategy = loadStrategy(combo.defined);
    const config = await Strategy.generate({ probe_timeout_ms: 5 }, hass);
    const rooms = config.views.find((v) => v.path === "rooms");
    const cards = allCards(rooms);
    const types = new Set(cards.map((c) => c.type));
    shapes[combo.label] = config.views.length;
    check(`caps(${combo.label}): still returns every view`, config.views.length === 13, `${config.views.length}`);
    for (const type of types) {
      if (!type.startsWith("custom:")) continue;
      check(
        `caps(${combo.label}): only emits ${type} when it is defined`,
        combo.defined.includes(type.replace("custom:", ""))
      );
    }
    const bedroom = averageIds(hass).find((a) => areaIdOf(hass, a) === "bedroom");
    const heroes = cards.filter((c) => entityIdsOf(c).includes(bedroom));
    check(`caps(${combo.label}): Bedroom still has a hero`, heroes.length > 0);
    if (combo.defined.includes("mushroom-template-card")) {
      check(
        `caps(${combo.label}): the hero is a mushroom card`,
        cards.some((c) => c.type === "custom:mushroom-template-card")
      );
      const topLevel = rooms.sections.flatMap((s) => s.cards).filter((c) => c.type !== "heading");
      check(
        `caps(${combo.label}): every hero fills its column`,
        topLevel.every((c) => c.type === "markdown" || c.grid_options?.columns === "full")
      );
      const graphs = cards.filter((c) => c.type === "custom:mini-graph-card");
      check(
        `caps(${combo.label}): sparkline follows mini-graph-card`,
        combo.defined.includes("mini-graph-card") ? graphs.length > 0 : graphs.length === 0
      );
      const stacks = cards.filter((c) => c.type === "custom:stack-in-card");
      check(
        `caps(${combo.label}): stacking follows stack-in-card`,
        combo.defined.includes("stack-in-card") && combo.defined.includes("mini-graph-card")
          ? stacks.length > 0
          : stacks.length === 0
      );
    } else {
      check(
        `caps(${combo.label}): falls back to core tiles`,
        !JSON.stringify(config).includes('"custom:')
      );
    }
  }

  /* the option overrides the probe in both directions */
  const Blind = loadStrategy([]);
  const forced = await Blind.generate({ cards: "mushroom" }, hass);
  check("caps: cards:mushroom trusts the option over the probe", JSON.stringify(forced).includes("custom:mushroom-template-card"));
  const Seeing = loadStrategy(CUSTOM_CARDS);
  const plain = await Seeing.generate({ cards: "native" }, hass);
  check("caps: cards:native ignores the installed cards", !JSON.stringify(plain).includes('"custom:'));
}

/* Colour must never be read out of the fixture: change every average's value
 * and the generated config must come back byte-identical. */
async function colourIndependence(Strategy, base) {
  const shifted = copy(base);
  for (const avg of averageIds(shifted)) shifted.states[avg].state = "12.34";
  const before = JSON.stringify(await Strategy.generate({ cards: "mushroom" }, base));
  const after = JSON.stringify(await Strategy.generate({ cards: "mushroom" }, shifted));
  check("colour: mushroom output does not depend on the current value", before === after);
  const beforeNative = JSON.stringify(await Strategy.generate({ cards: "native" }, base));
  const afterNative = JSON.stringify(await Strategy.generate({ cards: "native" }, shifted));
  check("colour: native output does not depend on the current value", beforeNative === afterNative);
}

/* Rooms whose data is missing entirely must still render. */
async function degradedRooms(Strategy, base) {
  const broken = copy(base);
  const avg = averageIds(broken).find((a) => areaIdOf(broken, a) === "bedroom");
  broken.states[avg].state = "unknown";
  broken.states[avg].attributes.reporting = 0;
  broken.states[avg].attributes.issue = "everything is on fire; " + "x".repeat(400);
  broken.states[avg].attributes.issues = ["a: offline", "b: offline"];
  for (const id of membersOf(broken, avg)) broken.states[id].state = "unavailable";
  const config = await Strategy.generate({}, broken);
  check("degraded: a fully dark room still generates", config.views.length > 0);
  const rooms = config.views.find((v) => v.path === "rooms");
  check(
    "degraded: the dark room still has a hero",
    allCards(rooms).some((c) => entityIdsOf(c).includes(avg))
  );
  const subview = config.views.find((v) => v.path === "room-bedroom");
  check("degraded: the dark room still has a subview", Boolean(subview));
  check(
    "degraded: no card secondary carries the raw issue string",
    !JSON.stringify(config).includes("everything is on fire")
  );
  const graph = allCards(subview).find((c) => c.type === "history-graph");
  check("degraded: history falls back to the average alone", graph.entities.length === 1);

  /* an average with no members at all */
  const empty = copy(base);
  const kitchen = averageIds(empty).find((a) => areaIdOf(empty, a) === "kitchen");
  empty.states[kitchen].attributes.sensors = [];
  empty.states[kitchen].attributes.excluded = [];
  empty.states[kitchen].attributes.member_count = 0;
  empty.states[kitchen].attributes.reporting = 0;
  const emptied = await Strategy.generate({}, empty);
  check("degraded: a memberless average still generates", emptied.views.length > 0);
  check(
    "degraded: a memberless subview still exists",
    emptied.views.some((v) => v.path === "room-kitchen")
  );

  /* no averages at all */
  const nothing = { states: {}, entities: {}, areas: {}, devices: {} };
  const bare = await Strategy.generate({}, nothing);
  check("degraded: an empty system still returns a view", bare.views.length === 1);
  check("degraded: the empty view has a path", Boolean(bare.views[0].path));
}

/* --------------------------------------------------------------- the run */

async function main() {
  if (!fs.existsSync(FIXTURE)) {
    console.error(`missing ${FIXTURE} — run: python3 tools/make_fixture.py`);
    process.exit(1);
  }
  const live = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
  const legacy = stripContract(live);
  const full = enrich(live);

  const backfilled = CONTRACT.filter(
    (name) => live.states[averageIds(live)[0]].attributes[name] === undefined
  );

  const modes = [
    { label: "mushroom + contract", defined: CUSTOM_CARDS, hass: full, custom: true, config: { cards: "auto" } },
    { label: "native + contract", defined: [], hass: full, custom: false, config: { cards: "auto" } },
    { label: "mushroom + legacy", defined: CUSTOM_CARDS, hass: legacy, custom: true, config: { cards: "auto" } },
    { label: "native + legacy", defined: [], hass: legacy, custom: false, config: { cards: "auto", probe_timeout_ms: 20 } },
  ];

  const shapes = [];
  let firstConfig = null;
  for (const mode of modes) {
    const Strategy = loadStrategy(mode.defined);
    check(`[${mode.label}] exposes static generate()`, typeof Strategy.generate === "function");
    const started = Date.now();
    const config = await Strategy.generate(mode.config, mode.hass);
    const elapsed = Date.now() - started;
    if (!firstConfig) firstConfig = config;
    assertAll(mode.label, config, mode.hass, mode);
    shapes.push({
      label: mode.label,
      views: config.views.length,
      paths: config.views.map((v) => v.path).sort().join(","),
      cards: config.views.reduce((n, v) => n + allCards(v).length, 0),
      elapsed,
    });
  }

  /* the probe must not change the shape of the dashboard, only its cards */
  for (const shape of shapes.slice(1)) {
    check(
      `[${shape.label}] same view count as the mushroom run`,
      shape.views === shapes[0].views,
      `${shape.views} vs ${shapes[0].views}`
    );
    check(
      `[${shape.label}] same view paths as the mushroom run`,
      shape.paths === shapes[0].paths
    );
  }

  const Defined = loadStrategy(CUSTOM_CARDS);
  await toggleChecks(Defined, live);
  await badgeChecks(Defined, full);
  await optionChecks(Defined, full);
  await nameChecks(Defined, full);
  await capabilityChecks(full);
  await colourIndependence(Defined, full);
  await degradedRooms(Defined, full);

  for (const shape of shapes) {
    console.log(
      `${shape.label.padEnd(22)} ${String(shape.views).padStart(2)} views, ` +
        `${String(shape.cards).padStart(3)} cards, ${shape.elapsed} ms`
    );
  }
  if (backfilled.length) {
    console.log(
      `fixture is missing ${backfilled.length} contract attribute(s) ` +
        `(${backfilled.join(", ")}) — synthesised for the contract runs`
    );
  } else {
    console.log("fixture carries every contract attribute");
  }
  console.log(`${checks.length} assertions, ${failures.length} failed`);

  if (process.argv.includes("--json")) {
    const out = path.join(HERE, "strategy_output.json");
    fs.writeFileSync(out, JSON.stringify(firstConfig, null, 2));
    console.log(`wrote ${out}`);
  }

  if (failures.length) {
    console.error("\nFAILURES:");
    for (const failure of failures.slice(0, 40)) console.error(`  ✗ ${failure}`);
    if (failures.length > 40) console.error(`  … ${failures.length - 40} more`);
    process.exit(1);
  }
  console.log("PASS");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
