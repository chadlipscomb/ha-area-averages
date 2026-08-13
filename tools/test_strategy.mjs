/* Exercise the dashboard strategy against a live-derived fixture, with no
 * browser and no login. Refresh the fixture with tools/make_fixture.py.
 *
 *   node tools/test_strategy.mjs            # assert + summary
 *   node tools/test_strategy.mjs --json     # also dump the generated config
 *
 * The suite runs the whole assertion set four times: with and without the
 * custom cards defined (mushroom-template-card, card-mod), and with and
 * without the contract attributes the integration publishes. The "legacy"
 * runs use a stripped fixture, which is what a browser sees when the JS ships
 * ahead of the Python.
 *
 * Shape under test — one section per area, each carrying:
 *   heading · reading card · [Sensors] [Offline] · member tiles · excluded
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

const SENSORS_SWITCH = "switch.area_averages_show_sensors";
const OFFLINE_SWITCH = "switch.area_averages_show_offline";
const OFFLINE = ["unavailable", "unknown"];
const CUSTOM_CARDS = ["mushroom-template-card", "card-mod"];
const CONTRACT = [
  "offline", "summary", "healthy", "minimum",
  "maximum", "spread", "trend", "trend_rate",
];
const BAND_COLORS = ["blue", "light-blue", "green", "amber", "red"];
const NO_COMFORT = ["3d_printer", "bench", "outside", "weather"];

const failures = [];
const checks = [];
function check(name, condition, detail = "") {
  checks.push(name);
  if (!condition) failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
}

const copy = (value) => JSON.parse(JSON.stringify(value));
const round2 = (value) => Math.round(value * 100) / 100;

/* ------------------------------------------------------------ strategy IO */

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

/* -------------------------------------------------------------- the model */

function averageIds(hass) {
  return Object.values(hass.entities || {})
    .filter((e) => e.platform === "area_averages")
    .filter((e) => hass.states[e.entity_id]?.attributes?.device_class === "temperature")
    .map((e) => e.entity_id)
    .sort();
}

const areaIdOf = (hass, avg) => hass.entities[avg].area_id;
const areaNameOf = (hass, avg) => hass.areas[areaIdOf(hass, avg)]?.name;
const membersOf = (hass, avg) => hass.states[avg].attributes.sensors || [];
const excludedOf = (hass, avg) => hass.states[avg].attributes.excluded || [];

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
    fill("spread", values.length > 1 ? round2(Math.max(...values) - Math.min(...values)) : null);
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

/* ------------------------------------------------------- section anatomy */

/* Split one room section into its parts, by position and by shape. */
function anatomy(section, hass, avg) {
  const cards = section.cards || [];
  const heading = cards[0];
  const buttons = cards.filter(
    (c) => c.entity === SENSORS_SWITCH || c.entity === OFFLINE_SWITCH
  );
  const reading = cards.filter(
    (c) => c.entity === avg && c.type !== "heading"
  );
  const memberIds = new Set(membersOf(hass, avg));
  const excludedIds = new Set(excludedOf(hass, avg));
  const members = cards.filter((c) => memberIds.has(c.entity));
  const excluded = cards.filter((c) => excludedIds.has(c.entity));
  return { heading, reading, buttons, members, excluded, cards };
}

const conditionsOf = (card) => JSON.stringify(card.visibility || []);

/* ----------------------------------------------------------- assertions */

function assertAll(Strategy, config, hass, label, mode) {
  const say = (name) => `[${label}] ${name}`;
  const rooms = config.views.find((v) => v.path === "rooms");
  check(say("has a Rooms view"), Boolean(rooms));
  check(say("has a Trends view"), Boolean(config.views.find((v) => v.path === "trends")));
  check(say("emits no subviews"), !config.views.some((v) => v.subview));

  const avgIds = averageIds(hass);
  const sections = rooms.sections || [];
  // One status section, then exactly one per area.
  check(
    say("one section per area, plus status"),
    sections.length === avgIds.length + 1,
    `${sections.length} for ${avgIds.length} areas`
  );

  for (const avg of avgIds) {
    const areaId = areaIdOf(hass, avg);
    const areaName = areaNameOf(hass, avg);
    const section = sections.find((s) => s.cards[0]?.heading === areaName);
    check(say(`${areaId} has its own section`), Boolean(section));
    if (!section) continue;

    const part = anatomy(section, hass, avg);

    /* --- the heading names the area --- */
    check(say(`${areaId} heading is a heading card`), part.heading.type === "heading");
    check(say(`${areaId} heading has an icon`), typeof part.heading.icon === "string");

    /* --- exactly one reading is ever visible --- */
    check(say(`${areaId} has a reading card`), part.reading.length > 0);
    if (mode.custom) {
      check(
        say(`${areaId} reading is a single mushroom card`),
        part.reading.length === 1 &&
          part.reading[0].type === "custom:mushroom-template-card"
      );
      const card = part.reading[0];
      // The temperature is the big line. Anything else there is a regression.
      check(
        say(`${areaId} reading leads with the temperature`),
        card.primary.includes("states(") && card.primary.includes("round(1)")
      );
      check(
        say(`${areaId} reading does not put the name in the big line`),
        !card.primary.includes(areaName)
      );
      check(
        say(`${areaId} secondary carries coverage`),
        card.secondary.includes("sensor")
      );
      check(
        say(`${areaId} reading spans its section`),
        card.grid_options?.columns === "full"
      );
      if (areaId === "fridge") {
        // Its own 45/50 ladder — a fridge at 68 °F is not "comfortable".
        check(
          say("fridge uses the cold ladder"),
          ["light-blue", "amber", "red"].every((c) => card.icon_color.includes(c)) &&
            !card.icon_color.includes("green"),
          card.icon_color.slice(0, 60)
        );
      } else if (!NO_COMFORT.includes(areaId)) {
        check(
          say(`${areaId} colour follows the ladder`),
          BAND_COLORS.every((c) => (card.icon_color || "").includes(c)),
          (card.icon_color || "").slice(0, 60)
        );
      }
    } else {
      // Core path: one tile per band plus an offline tile, mutually exclusive.
      const withColor = part.reading.filter((c) => c.color);
      check(say(`${areaId} native reading emits band tiles`), part.reading.length >= 1);
      for (const tile of withColor) {
        check(
          say(`${areaId} band tile ${tile.color} is conditional`),
          Array.isArray(tile.visibility) && tile.visibility.length > 0
        );
      }
    }

    /* --- two buttons, and what each one is for --- */
    const sensorsButton = part.buttons.find((c) => c.entity === SENSORS_SWITCH);
    const offlineButton = part.buttons.find((c) => c.entity === OFFLINE_SWITCH);
    if (membersOf(hass, avg).length) {
      check(say(`${areaId} has a Sensors button`), Boolean(sensorsButton));
      check(say(`${areaId} has an Offline button`), Boolean(offlineButton));
      if (sensorsButton) {
        check(
          say(`${areaId} Sensors button toggles`),
          sensorsButton.tap_action?.action === "toggle"
        );
        // Always available: it reveals sensors that are perfectly healthy.
        check(
          say(`${areaId} Sensors button is unconditional`),
          sensorsButton.visibility === undefined
        );
        check(
          say(`${areaId} Sensors button is half-width`),
          sensorsButton.grid_options?.columns === 6
        );
      }
      if (offlineButton) {
        check(
          say(`${areaId} Offline button toggles`),
          offlineButton.tap_action?.action === "toggle"
        );
        // Gated live on this room's own members, never on a snapshot.
        const flat = conditionsOf(offlineButton);
        check(
          say(`${areaId} Offline button watches its own members`),
          membersOf(hass, avg).every((id) => flat.includes(id))
        );
        const foreign = avgIds
          .filter((a) => a !== avg)
          .flatMap((a) => membersOf(hass, a))
          .filter((id) => !membersOf(hass, avg).includes(id));
        check(
          say(`${areaId} Offline button watches no other room`),
          !foreign.some((id) => flat.includes(id))
        );
      }
    } else {
      check(say(`${areaId} memberless room shows no buttons`), part.buttons.length === 0);
    }

    /* --- members hide behind the right button --- */
    check(
      say(`${areaId} renders every member`),
      part.members.length === membersOf(hass, avg).length,
      `${part.members.length}/${membersOf(hass, avg).length}`
    );
    for (const tile of part.members) {
      const flat = conditionsOf(tile);
      check(
        say(`${areaId} member ${tile.entity} is hidden by default`),
        flat.includes(SENSORS_SWITCH),
        "must be gated on the Sensors switch"
      );
      check(
        say(`${areaId} member ${tile.entity} is reachable via Offline`),
        flat.includes(OFFLINE_SWITCH) && flat.includes(tile.entity),
        "the Offline branch must name the member so a live death surfaces it"
      );
    }
    for (const tile of part.excluded) {
      check(
        say(`${areaId} excluded ${tile.entity} is greyed`),
        tile.color === "disabled"
      );
    }

    /* --- glass --- */
    if (mode.custom) {
      const styled = part.cards.filter((c) => c.card_mod);
      check(
        say(`${areaId} cards carry the glass treatment`),
        styled.length === part.cards.length - 1,
        `${styled.length}/${part.cards.length - 1} (heading is never glassed)`
      );
      for (const card of styled) {
        const css = JSON.stringify(card.card_mod.style);
        check(say(`${areaId} glass blurs`), css.includes("backdrop-filter"));
        check(say(`${areaId} glass is translucent`), css.includes("color-mix"));
        check(
          say(`${areaId} glass has a specular rim`),
          css.includes("inset 0 1px 0")
        );
        check(
          say(`${areaId} glass hardcodes no palette`),
          !/#[0-9a-f]{6}/i.test(css.replace(/#fff|#000/g, "")),
          "must theme through HA variables"
        );
      }
    } else {
      check(
        say(`${areaId} no card-mod without card-mod`),
        part.cards.every((c) => c.card_mod === undefined)
      );
    }
  }

  /* --- status --- */
  const status = sections[0];
  check(say("status is first"), status.cards.some((c) => c.type === "markdown"));
  check(say("status spans the view"), status.column_span === 3);

  /* --- global sanity --- */
  const cards = allCards(rooms);
  for (const card of cards) {
    check(say(`card has a type`), typeof card.type === "string");
    if (card.entity) {
      check(say(`entity ${card.entity} exists`), Boolean(hass.states[card.entity]));
    }
  }
  check(
    say("no graphs on the overview"),
    !cards.some((c) => /graph/.test(c.type || "")),
    "history belongs on Trends"
  );
  check(
    say("survives JSON round-trip"),
    JSON.stringify(JSON.parse(JSON.stringify(config))) === JSON.stringify(config)
  );
}

/* ---------------------------------------------------------------- driver */

async function main() {
  if (!fs.existsSync(FIXTURE)) {
    console.error(`missing ${FIXTURE} — run: python3 tools/make_fixture.py`);
    process.exit(1);
  }
  const live = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
  const contract = enrich(live);
  const legacy = stripContract(live);

  check(
    "fixture has both reveal switches",
    Boolean(live.states[SENSORS_SWITCH]) && Boolean(live.states[OFFLINE_SWITCH]),
    "run tools/make_fixture.py after deploying the integration"
  );

  const runs = [
    { label: "glass + contract", defined: CUSTOM_CARDS, hass: contract, custom: true },
    { label: "core + contract", defined: [], hass: contract, custom: false },
    { label: "glass + legacy", defined: CUSTOM_CARDS, hass: legacy, custom: true },
    { label: "core + legacy", defined: [], hass: legacy, custom: false },
  ];

  let last = null;
  for (const run of runs) {
    const Strategy = loadStrategy(run.defined);
    const started = Date.now();
    const config = await Strategy.generate({ probe_timeout_ms: 20 }, run.hass);
    const ms = Date.now() - started;
    assertAll(Strategy, config, run.hass, run.label, { custom: run.custom });
    const count = allCards(config.views[0]).length;
    console.log(
      `${run.label.padEnd(18)} ${config.views.length} views, ` +
        `${config.views[0].sections.length} sections, ${count} cards, ${ms} ms`
    );
    if (run.custom && run.hass === contract) last = config;
  }

  /* Turning glass off must change styling only, never structure. */
  const Strategy = loadStrategy(CUSTOM_CARDS);
  const flat = await Strategy.generate({ probe_timeout_ms: 20, glass: false }, contract);
  check(
    "glass:false emits no card-mod",
    !JSON.stringify(flat).includes("card_mod")
  );
  const strip = (c) => JSON.parse(JSON.stringify(c, (k, v) => (k === "card_mod" ? undefined : v)));
  check(
    "glass:false is style-only",
    JSON.stringify(strip(last)) === JSON.stringify(flat),
    "structure diverged between glass and flat"
  );

  console.log(`${checks.length} assertions, ${failures.length} failed`);

  if (process.argv.includes("--json")) {
    const out = path.join(HERE, "strategy_output.json");
    fs.writeFileSync(out, JSON.stringify(last, null, 2));
    console.log(`wrote ${out}`);
  }
  if (failures.length) {
    console.error("\nFAILURES:");
    const seen = new Set();
    for (const failure of failures) {
      const key = failure.replace(/sensor\.[a-z0-9_]+/g, "*");
      if (seen.has(key) && seen.size > 25) continue;
      seen.add(key);
      console.error(`  ✗ ${failure}`);
    }
    process.exit(1);
  }
  console.log("PASS");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
