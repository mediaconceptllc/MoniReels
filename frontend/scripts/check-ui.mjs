#!/usr/bin/env node
/**
 * Two interface rules that no type checker can hold, enforced mechanically.
 *
 * Both exist because the fix for each is a DEFAULT, and a default is only
 * worth having if the next component cannot quietly opt out of it. Neither
 * rule is a matter of taste: one is a published accessibility minimum, the
 * other is the difference between a page that keeps its shape while it loads
 * and a page that vanishes.
 *
 *   1. TARGETS. Every interactive element is at least 44 CSS px tall —
 *      either because it comes from a primitive that carries the floor
 *      (Button, TextInput, Select) or because it states a height itself.
 *      44 is Apple's published minimum; Material asks for 48. The default
 *      button was `py-2 text-sm`: 8 + 20 + 8 + 2 = 38px.
 *
 *   2. LOADING. `Spinner` belongs to a control that is working, never to a
 *      region that is loading. A region uses `Skeleton`, which holds the
 *      layout in place. Enforced by confining Spinner to ui.tsx, where
 *      Button renders it.
 *
 *   3. CARDS. `Card` is a SURFACE — a border, a radius and a background —
 *      and has never had padding of its own, because one card's content is
 *      a full-bleed thumbnail. Six call sites wrote `<Card>` with nothing
 *      else and rendered their text flush against the border. A card either
 *      states its padding or states that it is deliberately edge-to-edge.
 *
 * Run: npm run check-ui
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = join(import.meta.dirname, "..");
const SRC = join(ROOT, "src");
const UI = join(SRC, "components", "ui.tsx");

/** The standard this file holds the code to. It is written here rather than
 *  read from ui.tsx, so that lowering the constant in ui.tsx FAILS instead of
 *  quietly lowering the requirement along with it. */
const FLOOR_PX = 44;

const problems = [];
function fail(file, index, source, message) {
  const line = source.slice(0, index).split("\n").length;
  problems.push(`${relative(ROOT, file)}:${line}  ${message}`);
}

function tsxFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...tsxFiles(path));
    else if (entry.endsWith(".tsx")) out.push(path);
  }
  return out.sort();
}

/* ------------------------------------------------------------------ *
 * Reading JSX without parsing it
 * ------------------------------------------------------------------ */

/**
 * Comments are not markup.
 *
 * This file's own rules are explained in prose that quotes the very tags it
 * forbids, and a scanner that reads those quotations reports the
 * documentation as a violation of itself.
 *
 * It is one left-to-right pass rather than two regexes, because two regexes
 * cannot tell an opening `/*` from the same two characters inside a line
 * comment or a string. MEASURED: a `// … /admin/* is closed on the server`
 * comment on line 57 of the admin page opened a block comment that ran to
 * the next `*` + `/` sixty lines later, and the checker went blind over
 * every one of those lines — including the very page it was meant to hold.
 *
 * An apostrophe is NOT treated as a quote. This codebase writes strings with
 * double quotes and template literals, while `'` appears in ordinary English
 * prose ("the producer's terms"); reading one as an opening quote would
 * blind the scanner to everything up to the next one, which is the same bug
 * in a different disguise. A stray `//` inside a single-quoted string can
 * then blank the rest of that line, which is harmless — a tag written inside
 * a string is not a tag.
 */
function withoutComments(source) {
  let out = "";
  let i = 0;
  let quote = null;
  while (i < source.length) {
    const ch = source[i];
    const next = source[i + 1];
    if (quote) {
      if (ch === "\\") {
        out += ch + (next ?? "");
        i += 2;
        continue;
      }
      if (ch === quote) quote = null;
      out += ch;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "`") {
      quote = ch;
      out += ch;
      i += 1;
      continue;
    }
    if (ch === "/" && next === "/") {
      while (i < source.length && source[i] !== "\n") {
        out += " ";
        i += 1;
      }
      continue;
    }
    if (ch === "/" && next === "*") {
      while (i < source.length && !(source[i] === "*" && source[i + 1] === "/")) {
        out += source[i] === "\n" ? "\n" : " ";
        i += 1;
      }
      out += "  ";
      i += 2;
      continue;
    }
    out += ch;
    i += 1;
  }
  return out;
}

/** Yields each opening tag of interest as raw text. Walks the source rather
 *  than matching `<tag[^>]*>`, because a className built from a template
 *  literal contains `>` inside `${...}` and a lazy regex stops there. */
function* openingTags(source) {
  const re = /<(button|a|select|textarea|input|summary|Link)(?=[\s/>])/g;
  let match;
  while ((match = re.exec(source))) {
    let i = re.lastIndex;
    let depth = 0;
    let quote = null;
    while (i < source.length) {
      const ch = source[i];
      if (quote) {
        if (ch === "\\") i += 1;
        else if (ch === quote) quote = null;
      } else if (ch === '"' || ch === "'" || ch === "`") {
        quote = ch;
      } else if (ch === "{") {
        depth += 1;
      } else if (ch === "}") {
        depth -= 1;
      } else if (ch === ">" && depth === 0) {
        break;
      }
      i += 1;
    }
    yield { tag: match[1], text: source.slice(match.index, i + 1), index: match.index };
  }
}

/** `const SELECT = "…"` at file level, so `className={SELECT}` can be read. */
function stringConstants(source) {
  const consts = new Map();
  const re = /^const\s+([A-Za-z_$][\w$]*)\s*=\s*"([^"]*)"/gm;
  let match;
  while ((match = re.exec(source))) consts.set(match[1], match[2]);
  return consts;
}

function classesOf(tagText, consts) {
  const quoted = /className="([^"]*)"/.exec(tagText);
  if (quoted) return quoted[1];
  const templated = /className=\{`([\s\S]*?)`/.exec(tagText);
  if (templated) return templated[1];
  const braced = /className=\{([^}]*)\}/.exec(tagText);
  if (braced) {
    const name = braced[1].trim();
    if (consts.has(name)) return consts.get(name);
    return braced[1];
  }
  return "";
}

/* ------------------------------------------------------------------ *
 * Rule 1 — targets
 * ------------------------------------------------------------------ */

/** Tailwind's spacing scale is 0.25rem per step, and the root font size is
 *  16px — so `h-11` is 44px. Arbitrary values are read as written. */
function heightPx(token) {
  const match = /^(?:min-)?h-(?:\[(\d+(?:\.\d+)?)(px|rem)\]|(\d+(?:\.\d+)?))$/.exec(token);
  if (!match) return null;
  if (match[3] !== undefined) return Number(match[3]) * 4;
  return match[2] === "rem" ? Number(match[1]) * 16 : Number(match[1]);
}

function hasFloor(classes) {
  // `${TAP}` is the floor by definition — the constant is checked separately.
  if (/\$\{TAP\}|\bTAP\b/.test(classes)) return true;
  return classes
    .split(/[\s`]+/)
    .some((token) => (heightPx(token) ?? 0) >= FLOOR_PX);
}

/** Not a target at all: a file input triggered by a button next to it, or
 *  anything else deliberately removed from the layout. */
function isHidden(tagText, classes) {
  const tokens = classes.split(/[\s`]+/);
  if (tokens.includes("sr-only") || tokens.includes("hidden")) return true;
  return /type="hidden"/.test(tagText);
}

function checkTargets(file, source, isPrimitives) {
  const consts = stringConstants(source);
  for (const { tag, text, index } of openingTags(source)) {
    const classes = classesOf(text, consts);
    if (isHidden(text, classes)) continue;

    // A checkbox is never the target on its own — the words beside it are
    // part of what gets tapped, so the floor belongs to the LABEL around
    // both. That pair is the Checkbox primitive; nowhere else draws one.
    if (/type="(?:checkbox|radio)"/.test(text)) {
      if (!isPrimitives) {
        fail(file, index, source, `<${tag} type="checkbox"> — use the Checkbox primitive, which puts the floor on the label around the box and its words.`);
      }
      continue;
    }

    if (hasFloor(classes)) continue;
    fail(
      file,
      index,
      source,
      `<${tag}> has no ${FLOOR_PX}px target height. Use the Button / TextInput / Select ` +
        `primitive, or add \`\${TAP}\` to its className. If it is a link INSIDE a ` +
        `sentence, do not stretch it — that breaks the line it sits in; make it a ` +
        `real action beside the text, or extend this rule with the WCAG 2.2 inline ` +
        `exemption.`,
    );
  }
}

/** The primitives are where the floor actually lives, so they are checked by
 *  name: a Button that stopped carrying TAP would leave every call site short
 *  while every call site still looked correct. */
function checkPrimitives(source) {
  const declared = /^export const TAP = "([^"]+)";$/m.exec(source);
  if (!declared) {
    problems.push(`src/components/ui.tsx  TAP is not declared as a single string constant.`);
    return;
  }
  const px = heightPx(declared[1]);
  if (px === null || px < FLOOR_PX) {
    problems.push(
      `src/components/ui.tsx  TAP is \`${declared[1]}\` (${px ?? "?"}px), below the ${FLOOR_PX}px floor.`,
    );
  }
  for (const name of ["Button", "TextInput", "Select"]) {
    const body = new RegExp(`(?:function|const)\\s+${name}\\b[\\s\\S]*?\\n\\}`).exec(source);
    if (!body) {
      problems.push(`src/components/ui.tsx  primitive \`${name}\` is missing.`);
    } else if (!/\$\{TAP\}/.test(body[0])) {
      problems.push(`src/components/ui.tsx  primitive \`${name}\` does not carry \${TAP}.`);
    }
  }
}

/* ------------------------------------------------------------------ *
 * Rule 3 — cards
 * ------------------------------------------------------------------ */

function checkCards(file, source) {
  const consts = stringConstants(source);
  const re = /<Card(?=[\s/>])/g;
  let match;
  while ((match = re.exec(source))) {
    // Reuse the same walker: a Card's className is a template literal often
    // enough that a lazy regex would stop inside one.
    let i = match.index;
    let depth = 0;
    let quote = null;
    while (i < source.length) {
      const ch = source[i];
      if (quote) {
        if (ch === "\\") i += 1;
        else if (ch === quote) quote = null;
      } else if (ch === '"' || ch === "'" || ch === "`") quote = ch;
      else if (ch === "{") depth += 1;
      else if (ch === "}") depth -= 1;
      else if (ch === ">" && depth === 0) break;
      i += 1;
    }
    const classes = classesOf(source.slice(match.index, i + 1), consts);
    const tokens = classes.split(/[\s`]+/);
    const padded = tokens.some((t) => /^p[xytrbl]?-/.test(t));
    // The one legitimate unpadded card clips a thumbnail to its own corners,
    // which is what `overflow-hidden` is there for — so saying it is also
    // how a card declares that it meant to be edge-to-edge.
    const fullBleed = tokens.includes("overflow-hidden");
    if (!padded && !fullBleed) {
      fail(file, match.index, source, "<Card> states no padding. Add `p-5` (or `overflow-hidden` if its content really is edge-to-edge).");
    }
  }
}

/* ------------------------------------------------------------------ *
 * Rule 2 — loading
 * ------------------------------------------------------------------ */

function checkSpinner(file, source) {
  const match = /\bSpinner\b/.exec(source);
  if (!match) return;
  fail(
    file,
    match.index,
    source,
    "Spinner is for a control that is working, not a region that is loading. " +
      "Use Skeleton, which keeps the layout in place.",
  );
}

/* ------------------------------------------------------------------ */

const files = tsxFiles(SRC);
checkPrimitives(readFileSync(UI, "utf8"));
for (const file of files) {
  const source = withoutComments(readFileSync(file, "utf8"));
  checkTargets(file, source, file === UI);
  checkCards(file, source);
  if (file !== UI) checkSpinner(file, source);
}

if (problems.length) {
  console.error(`check-ui: ${problems.length} problem(s)\n`);
  for (const problem of problems) console.error("  " + problem);
  console.error("");
  process.exit(1);
}
console.log(
  `check-ui: ${files.length} files, every target at least ${FLOOR_PX}px, ` +
    "no stray Spinner, every Card padded.",
);
