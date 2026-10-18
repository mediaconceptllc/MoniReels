/**
 * Every message the client builds for a person reads in Mongolian.
 *
 * Three places turned machine text into screen text and let English through:
 * a failed job showed the worker's own words ("Not enough scratch space…");
 * relative times came from Intl.RelativeTimeFormat("mn"), which Chromium has
 * no Mongolian data for ("-1 h", "yesterday"); and a refused field showed
 * FastAPI's English ("String should have at most 200 characters"). Each is
 * checked here against the real inputs: the error strings are copied from the
 * backend's raise sites, so a new one that matches nothing fails this check.
 *
 * Run: npm run check-copy
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { explainJobError } from "../src/lib/job-errors";
import { relativeTime } from "../src/lib/format";
import { describeValidation } from "../src/lib/messages";

const problems: string[] = [];
const LATIN = /[A-Za-z]{3,}/;

// What the worker and the reaper actually write into job.error.
const WORKER_ERRORS: [string, boolean][] = [
  ["OutOfSpace: Not enough scratch space: 1.84 GB free, need 4.20 GB", true],
  ["Worker stopped responding after 3 attempt(s).", true],
  [
    "Worker stopped responding. This job type is not retried automatically because each attempt is billed separately.",
    true,
  ],
  ["The project was deleted", false],
  ["RuntimeError: This video has no audio track", false],
  ["OpenRouterError: OPENROUTER_API_KEY is not configured", false],
  ["DuudlagaError: DUUDLAGA_BASE_URL / DUUDLAGA_API_KEY are not configured", false],
  ["TtsError: ElevenLabs API түлхүүр тавигдаагүй байна.", false],
  ["OpenRouterError: OpenRouter request failed (401)", false],
  ["OpenRouterError: OpenRouter request failed (402)", false],
  ["OpenRouterError: OpenRouter request failed (429)", true],
  ["OpenRouterError: OpenRouter request failed (502)", true],
  ["OpenRouterError: OpenRouter request failed: ReadTimeout", true],
  ["SuggestionValidationError: Response did not match the required JSON shape: x", true],
  ["SuggestionValidationError: No short came back at all", true],
  ["TranslationError: None of the 40 line(s) could be translated", true],
  ["ProbeError: No video stream found in /work/source.mp4", false],
  ["RuntimeError: This project has no uploaded source video", false],
  ["RuntimeError: Transcribe the video before asking for suggestions", false],
  ["RuntimeError: This video is already in Mongolian", false],
  ["RuntimeError: There are no suggestions to export", false],
  ["RuntimeError: This project has no video to transcribe yet", false],
  ["RuntimeError: Transcribe the video before translating it", false],
  ["RuntimeError: The transcript was removed while it was being translated", true],
  ["RuntimeError: None of the selected ideas are still in this project: Богино 2", false],
  ["OverlayError: Logo opacity must be between 0 and 1, got 3", false],
  ["TtsError: ElevenLabs дуу үүсгэж чадсангүй (401): invalid key", false],
  ["TtsError: ElevenLabs дуу үүсгэж чадсангүй (429)", true],
  ["TtsError: Монгол дууны хоолой сонгогдоогүй байна.", false],
  // The model's own words ride along in these; none of them is a billing problem.
  ['OpenRouterError: Response was not valid JSON: {"summary": "work-life balance, credits roll"', true],
  ["SuggestionValidationError: Short 'Forbidden love': no cut with role 'hook'", true],
];

// Already Mongolian, no rule for the cause: the client's own words, unprefixed.
const own = explainJobError("DuudlagaError: duudlaga.dev API түлхүүрийг татгалзлаа. DUUDLAGA_API_KEY-г шалгана уу.");
if (own.message.startsWith("Тодорхойгүй") || own.message.startsWith("DuudlagaError")) {
  problems.push(`Mongolian worker error not passed through: ${own.message}`);
}

for (const [error, retry] of WORKER_ERRORS) {
  const explained = explainJobError(error);
  if (explained.message.startsWith("Тодорхойгүй")) problems.push(`job error not recognised: ${error}`);
  if (LATIN.test(explained.message.replace(/R2|YouTube/g, ""))) {
    problems.push(`job error explained with Latin text: ${explained.message}`);
  }
  if (explained.retry !== retry) problems.push(`job error retry=${explained.retry}, want ${retry}: ${error}`);
}
if (!explainJobError("something nobody has seen").retry) problems.push("unknown errors should allow a retry");

// Relative times, across every unit, in both directions.
const now = Date.UTC(2026, 9, 18, 12, 0, 0);
for (const minutes of [0, 1, 5, 59, 60, 61, 300, 1439, 1440, 2880, 10080, 43200, 100000]) {
  for (const sign of [-1, 1]) {
    const text = relativeTime((now + sign * minutes * 60000) / 1000, now);
    if (LATIN.test(text)) problems.push(`relative time in Latin: ${minutes * sign} min → ${text}`);
  }
}
if (relativeTime((now - 5 * 60000) / 1000, now) !== "5 минутын өмнө") problems.push("5 minutes ago misread");
if (relativeTime((now - 26 * 3600000) / 1000, now) !== "өчигдөр") problems.push("yesterday misread");

// Node ships Mongolian locale data and Chromium does not: in Node,
// Intl.RelativeTimeFormat("mn") says "1 цагийн өмнө", in the browser the same
// call resolves to en-US and says "1 hour ago" — and so does every Intl call
// for "mn" (MEASURED in Chromium: month "October", weekday "Saturday"), along
// with toLocaleString("mn-MN"), which is Intl underneath. The behaviour check
// above therefore cannot see that regression — only the source can.
function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? sources(path) : /\.tsx?$/.test(name) ? [path] : [];
  });
}
for (const path of sources(join(__dirname, "..", "src"))) {
  // Comments are dropped first: format.ts says, in one, what it replaced.
  const code = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
  if (
    /Intl\.(RelativeTimeFormat|DateTimeFormat|NumberFormat|ListFormat|PluralRules)\(\s*["']mn/.test(code) ||
    /\.toLocale(?:Date|Time)?String\(\s*["']mn/.test(code)
  ) {
    problems.push(`${path}: Intl with "mn" falls back to English in Chromium — write the words out`);
  }
}

// FastAPI's validation list, as the server's handler returns it.
const refused = describeValidation([
  { type: "string_too_long", loc: ["body", "name"], msg: "String should have at most 200 characters" },
  { type: "missing", loc: ["body", "new_password"], msg: "Field required" },
  { type: "value_error", loc: ["body", "filename"], msg: "Value error, Энэ файлын төрөл дэмжигдэхгүй (.txt)." },
  { type: "something_new", loc: ["body", "x"], msg: "Input should be valid" },
]);
if (/should|required|Value error/i.test(refused)) problems.push(`validation left English in: ${refused}`);

if (problems.length) {
  console.error(`check-copy: ${problems.length} problem(s)\n  ${problems.join("\n  ")}`);
  process.exit(1);
}
console.log(
  `check-copy: ${WORKER_ERRORS.length} worker errors explained in Mongolian, relative times and validation messages too.`,
);
