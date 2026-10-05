/* Render every Jinja template the strategy emits against the live HA template
 * API and assert each one compiles and produces output.
 *
 *   node tools/render_check.mjs          # summary
 *   node tools/render_check.mjs --show   # print each rendered result
 *
 * Why this exists: tools/test_strategy.mjs only substring-matches the emitted
 * templates, so an unbalanced {% endif %} in a colour ladder would pass every
 * structural assertion and then render an error string on every hero card. The
 * mushroom path is the usual live path, which puts essentially all of
 * the dashboard's colour and secondary-text logic behind Jinja that the node
 * harness cannot evaluate. This closes that gap.
 *
 * Exit code 1 if any template fails to render.
 */

import fs from "node:fs";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");
const STRATEGY = path.join(REPO, "www", "area_averages_strategy.js");
const FIXTURE = path.join(HERE, "fixture.json");
const BASE = (process.env.HA_URL || "https://homeassistant.local:8123").replace(/\/$/, "");
const TOKEN =
  process.env.HA_TOKEN ||
  fs.readFileSync(path.join(os.homedir(), ".config", "ha-token"), "utf8").trim();

/* Card options whose values are Jinja, keyed by how HA treats them. */
const TEMPLATE_KEYS = [
  "content",
  "primary",
  "secondary",
  "icon_color",
  "badge_icon",
  "badge_color",
  "icon",
  "state_content",
  "name",
  "multiline_secondary",
];

function renderTemplate(template) {
  return new Promise((resolve) => {
    const body = JSON.stringify({ template });
    const req = https.request(
      `${BASE}/api/template`,
      {
        method: "POST",
        rejectUnauthorized: false,
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
        timeout: 20000,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => resolve({ status: res.statusCode, body: data }));
      }
    );
    req.on("error", (err) => resolve({ status: 0, body: String(err) }));
    req.on("timeout", () => {
      req.destroy();
      resolve({ status: 0, body: "timeout" });
    });
    req.write(body);
    req.end();
  });
}

function loadStrategy() {
  const code = fs.readFileSync(STRATEGY, "utf8");
  const registry = {};
  const shim = { define: (name, cls) => (registry[name] = cls) };
  const quiet = { info() {}, warn() {}, error() {}, log() {} };
  new Function("customElements", "console", code)(shim, quiet);
  return registry[Object.keys(registry)[0]];
}

/* Collect every string in the generated config that contains Jinja. */
function collectTemplates(node, trail, found) {
  if (typeof node === "string") {
    if (node.includes("{{") || node.includes("{%")) {
      found.push({ where: trail, template: node });
    }
    return;
  }
  if (Array.isArray(node)) {
    node.forEach((item, i) => collectTemplates(item, `${trail}[${i}]`, found));
    return;
  }
  if (node && typeof node === "object") {
    for (const [key, value] of Object.entries(node)) {
      collectTemplates(value, trail ? `${trail}.${key}` : key, found);
    }
  }
}

async function main() {
  const hass = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
  const Strategy = loadStrategy();

  const variants = [
    ["auto", {}],
    ["mushroom", { cards: "mushroom" }],
    ["native", { cards: "native" }],
  ];

  const seen = new Map();
  for (const [label, opts] of variants) {
    const config = await Strategy.generate({ ...opts }, hass);
    const found = [];
    collectTemplates(config, "", found);
    for (const item of found) {
      if (!seen.has(item.template)) {
        seen.set(item.template, { ...item, variant: label });
      }
    }
  }

  const templates = [...seen.values()];
  if (!templates.length) {
    console.error("no templates found in generated config — is the strategy emitting Jinja?");
    process.exit(1);
  }
  console.log(`rendering ${templates.length} distinct templates against ${BASE} ...`);

  const failures = [];
  let rendered = 0;
  for (const item of templates) {
    const res = await renderTemplate(item.template);
    const looksBroken =
      res.status !== 200 ||
      /TemplateSyntaxError|UndefinedError|TemplateError|has no attribute|is undefined/i.test(
        res.body
      );
    if (looksBroken) {
      failures.push({ ...item, status: res.status, body: res.body.slice(0, 400) });
    } else {
      rendered += 1;
      if (process.argv.includes("--show")) {
        console.log(`  ${item.where} [${item.variant}] -> ${res.body.trim().slice(0, 140)}`);
      }
    }
  }

  console.log(`${rendered}/${templates.length} rendered cleanly`);
  if (failures.length) {
    console.error("\nFAILURES:");
    for (const f of failures) {
      console.error(`  ✗ ${f.where} [${f.variant}] HTTP ${f.status}`);
      console.error(`    ${f.template.replace(/\s+/g, " ").slice(0, 200)}`);
      console.error(`    ${f.body.replace(/\s+/g, " ")}`);
    }
    process.exit(1);
  }
  console.log("PASS");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
