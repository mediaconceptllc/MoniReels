/**
 * The security headers vercel.json sends, held to what they are for.
 *
 * `next start` does not read vercel.json — only Vercel does — so nothing in
 * typecheck, lint or build ever sees these headers, and a policy that blocks
 * the API would first be noticed by a user staring at "Серверт холбогдож
 * чадсангүй". The policy was measured against a real `next build` + `next
 * start` with the headers applied (docs/DEPLOY.md, «Аюулгүй байдлын
 * толгой»): zero violations on login, the list, a project with video and
 * outputs, the admin screens, and a single and a multipart upload. This
 * check keeps the next edit inside what was measured:
 *
 *   - connect-src names the API the build points at (build.env), and R2 —
 *     change NEXT_PUBLIC_API_URL without it and every request is blocked;
 *   - img-src and media-src name R2: every thumbnail and video is a
 *     presigned R2 URL;
 *   - scripts come from this origin only: no eval, no other host, no
 *     scheme-wide source. 'unsafe-inline' is the one allowance, and only
 *     because App Router's flight data is inline <script> — without it the
 *     measured build never hydrated (57 violations, no usable login form);
 *   - styles need no allowance at all, so none may be added;
 *   - framing, plugins, <base> and form targets are shut.
 *
 * Run: npm run check-headers [path/to/vercel.json]
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

type Header = { key: string; value: string };
type VercelJson = {
  headers?: { source: string; headers: Header[] }[];
  build?: { env?: Record<string, string> };
};

const file = resolve(process.argv[2] ?? "vercel.json");
const conf = JSON.parse(readFileSync(file, "utf8")) as VercelJson;

const problems: string[] = [];
function expect(name: string, ok: boolean, extra = "") {
  if (!ok) problems.push(`${name}${extra ? ` — ${extra}` : ""}`);
}

const everywhere = (conf.headers ?? []).find((rule) => rule.source === "/(.*)");
expect("one rule covers every path (/(.*))", Boolean(everywhere));
const headers = new Map((everywhere?.headers ?? []).map((h) => [h.key.toLowerCase(), h.value]));

expect(
  "the policy is enforced, not report-only",
  !headers.has("content-security-policy-report-only"),
);
const csp = headers.get("content-security-policy") ?? "";
expect("Content-Security-Policy is set", csp.length > 0);

const directives = new Map<string, string[]>();
for (const part of csp.split(";")) {
  const [name, ...sources] = part.trim().split(/\s+/);
  if (!name) continue;
  expect(`${name} is declared once`, !directives.has(name));
  directives.set(name, sources);
}
const sources = (name: string) => directives.get(name) ?? [];
const has = (name: string, source: string) => sources(name).includes(source);

const R2 = "https://*.r2.cloudflarestorage.com";
const api = conf.build?.env?.NEXT_PUBLIC_API_URL;
expect("build.env names the API", Boolean(api));
const apiOrigin = api ? new URL(api).origin : "";

expect("default-src is 'self' only", sources("default-src").join(" ") === "'self'", sources("default-src").join(" "));
expect("connect-src names the API the build calls", has("connect-src", apiOrigin), `${apiOrigin} not in connect-src`);
for (const name of ["connect-src", "img-src", "media-src"]) {
  expect(`${name} names R2`, has(name, R2));
}

const script = sources("script-src");
expect("script-src includes 'self'", script.includes("'self'"));
expect(
  "script-src allows no eval",
  !script.some((s) => s === "'unsafe-eval'" || s === "'wasm-unsafe-eval'"),
);
expect(
  "script-src names no other host or scheme",
  script.every((s) => s.startsWith("'")),
  script.filter((s) => !s.startsWith("'")).join(" "),
);
expect(
  "style-src is 'self' only — the measured build needed no inline style",
  sources("style-src").join(" ") === "'self'",
  sources("style-src").join(" "),
);

for (const [name, list] of directives) {
  const wide = list.filter((s) => s === "*" || /^[a-z][a-z0-9+.-]*:$/i.test(s));
  expect(`${name} has no wildcard or scheme-wide source`, wide.length === 0, wide.join(" "));
}

expect("frame-ancestors 'none'", sources("frame-ancestors").join(" ") === "'none'");
expect("object-src 'none'", sources("object-src").join(" ") === "'none'");
expect("frame-src 'none'", sources("frame-src").join(" ") === "'none'");
expect("base-uri 'self'", sources("base-uri").join(" ") === "'self'");
expect("form-action 'self'", sources("form-action").join(" ") === "'self'");

const permissions = headers.get("permissions-policy") ?? "";
for (const feature of ["camera", "microphone", "geolocation", "payment", "usb", "display-capture"]) {
  expect(`Permissions-Policy turns off ${feature}`, permissions.split(/,\s*/).includes(`${feature}=()`));
}

const hsts = headers.get("strict-transport-security") ?? "";
const maxAge = Number(/max-age=(\d+)/.exec(hsts)?.[1] ?? 0);
expect("HSTS lasts at least a year", maxAge >= 31_536_000, hsts);
expect("HSTS covers subdomains", /includeSubDomains/i.test(hsts), hsts);

expect("X-Frame-Options DENY (older browsers)", headers.get("x-frame-options") === "DENY");
expect("X-Content-Type-Options nosniff", headers.get("x-content-type-options") === "nosniff");
expect("Referrer-Policy set", Boolean(headers.get("referrer-policy")));

if (problems.length) {
  console.error(`check-headers: ${problems.length} problem(s) in ${file}\n  ${problems.join("\n  ")}`);
  process.exit(1);
}
console.log(
  `check-headers: CSP enforced (${directives.size} directives), API ${apiOrigin} and R2 reachable, ` +
    "scripts from this origin only, framing shut, HSTS and Permissions-Policy set.",
);
