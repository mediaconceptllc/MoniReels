#!/usr/bin/env node
/**
 * `npm audit --audit-level=high`, with a list of advisories this project has
 * decided to live with — each one named, with the reason (audit DEP-1).
 *
 * npm audit itself cannot ignore anything: one advisory nobody can fix fails
 * every build, and the step gets switched off, which hides the next one too.
 * So: every HIGH or CRITICAL advisory blocks unless it is in IGNORED below,
 * where the reason sits next to it for the next reader to re-check. Moderate
 * and low are listed in the job summary and do not block.
 *
 * An ignore that no longer matches anything is reported, not failed on:
 * it means the fix arrived and the entry should go.
 */
import { spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";

const IGNORED = {
  // braces <= 3.0.3 — stack exhaustion on deeply nested patterns. Every
  // published braces is affected; there is no fix to move to. It reaches us
  // only through eslint-config-next -> @next/eslint-plugin-next -> fast-glob
  // -> micromatch: the linter, a dev dependency, never the built app, and
  // its patterns are this repository's own lint globs. npm's suggested
  // "fix" is a downgrade to eslint-config-next 14. Re-check when braces,
  // micromatch or fast-glob releases.
  "GHSA-vfj7-8cjw-p6xm": "braces: no fixed release; lint-only path, our own globs",
};

const BLOCKING = new Set(["high", "critical"]);

const run = spawnSync("npm", ["audit", "--json"], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
let report;
try {
  report = JSON.parse(run.stdout);
} catch {
  console.error(run.stdout, run.stderr);
  console.error("check-audit: npm audit did not return JSON");
  process.exit(2);
}
if (report.error) {
  console.error(`check-audit: npm audit failed: ${report.error.summary || JSON.stringify(report.error)}`);
  process.exit(2);
}

// The advisories themselves. A package flagged only because it depends on
// a flagged package carries a string in `via`; the advisory is an object.
const advisories = new Map();
for (const [pkg, vuln] of Object.entries(report.vulnerabilities || {})) {
  for (const via of vuln.via) {
    if (typeof via !== "object") continue;
    const id = String(via.url || via.source).split("/").pop();
    const seen = advisories.get(id) || { id, title: via.title, severity: via.severity, packages: new Set() };
    seen.packages.add(`${pkg} ${via.range}`);
    advisories.set(id, seen);
  }
}

const rows = [...advisories.values()].map((a) => ({
  ...a,
  status: !BLOCKING.has(a.severity) ? "below the bar" : IGNORED[a.id] ? `ignored: ${IGNORED[a.id]}` : "BLOCKING",
}));
const blocking = rows.filter((r) => r.status === "BLOCKING");
const stale = Object.keys(IGNORED).filter((id) => !advisories.has(id));

const summary = [
  "### npm audit",
  "",
  rows.length ? "| advisory | severity | package | status |" : "No advisories.",
  ...(rows.length ? ["|---|---|---|---|"] : []),
  ...rows.map((r) => `| [${r.id}](https://github.com/advisories/${r.id}) ${r.title} | ${r.severity} | ${[...r.packages].join(", ")} | ${r.status} |`),
  ...stale.map((id) => `\nIgnored but no longer reported — remove it from scripts/check-audit.mjs: ${id}`),
  "",
].join("\n");
console.log(summary);
if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary + "\n");

// As annotations too: they come with the check run, the log does not.
const escape = (text) => text.replace(/%/g, "%25").replace(/\r/g, "%0D").replace(/\n/g, "%0A");
if (process.env.GITHUB_ACTIONS === "true") {
  for (const r of blocking) {
    console.log(`::error title=npm audit::${escape(`${r.id} (${r.severity}) ${r.title} — ${[...r.packages].join(", ")}`)}`);
  }
  for (const id of stale) console.log(`::warning title=npm audit::${id} is ignored but no longer reported`);
}

if (blocking.length) {
  console.error(`check-audit: ${blocking.length} high or critical advisor${blocking.length === 1 ? "y" : "ies"} not ignored`);
  process.exit(1);
}
console.log(`check-audit: nothing high or critical outside the ${Object.keys(IGNORED).length} named ignore(s).`);
