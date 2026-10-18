/**
 * The job stream's parser, fed what the server really sends.
 *
 * The frames below are sse-starlette's own bytes (`ServerSentEvent.encode()`
 * in the backend's pinned version): CRLF line endings. The parser this
 * replaced split on "\n\n" and read none of them, and nothing failed — the
 * page fell back to polling after eight seconds and looked merely slow. So
 * this checks the bytes, and every place a network chunk can cut them,
 * including between the CR and the LF.
 *
 * Run: npm run check-sse
 */
import { takeEvents } from "../src/lib/sse";

const problems: string[] = [];
function expect(name: string, got: unknown, want: unknown) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    problems.push(`${name}\n    got  ${JSON.stringify(got)}\n    want ${JSON.stringify(want)}`);
  }
}

/** Feed `chunks` one at a time, as a reader would, and collect every event. */
function read(chunks: string[]): string[] {
  let buffer = "";
  const out: string[] = [];
  for (const chunk of chunks) {
    const { data, rest } = takeEvents(buffer + chunk);
    out.push(...data);
    buffer = rest;
  }
  return out;
}

const running = '{"state": "running"}';
const done = '{"state": "done"}';
// What sse-starlette writes: an event, a keep-alive comment, an event.
const crlf = `event: job\r\ndata: ${running}\r\n\r\n: ping\r\n\r\nevent: job\r\ndata: ${done}\r\n\r\n`;

expect("CRLF frames in one chunk", read([crlf]), [running, done]);
expect("LF frames", read([crlf.replace(/\r\n/g, "\n")]), [running, done]);
// A CR ending a chunk waits for the next byte (it may be half a CRLF), so the
// last lone-CR event is read once anything at all follows it — the server's
// keep-alive comment does that within seconds.
expect("lone-CR frames", read([crlf.replace(/\r\n/g, "\r"), ": ping\r"]), [running, done]);

// Every cut a network can make, including CR | LF and inside "\r\n\r\n".
for (let at = 1; at < crlf.length; at++) {
  expect(`CRLF cut at byte ${at}`, read([crlf.slice(0, at), crlf.slice(at)]), [running, done]);
}
// One byte at a time: the hardest case for a held-back CR.
expect("CRLF one byte per chunk", read([...crlf]), [running, done]);

// An unfinished event waits for the rest; it is not read half-formed.
expect("unfinished event is held", takeEvents(`data: ${running}\r\n`).data, []);
// Several data lines are one value, joined by a newline (the spec).
expect("multi-line data", read(["data: a\r\ndata: b\r\n\r\n"]), ["a\nb"]);

if (problems.length) {
  console.error(`check-sse: ${problems.length} problem(s)\n  ${problems.join("\n  ")}`);
  process.exit(1);
}
console.log(`check-sse: CRLF, LF and CR frames read, at every chunk boundary (${crlf.length - 1} cuts).`);
