/**
 * Reading a server-sent event stream out of fetch chunks.
 *
 * Kept apart from `jobs.ts` so it can be checked without a browser
 * (`npm run check-sse`): it is the one place where a byte-level mistake turns
 * a working feature off without an error. It did. The server (sse-starlette)
 * ends every line with CRLF, as the spec allows, and this used to split on a
 * bare "\n\n" — which never occurs in "\r\n\r\n". No event was ever read; each
 * job sat on «Ажлын төлөв уншиж байна…» for the eight-second fallback and then
 * polled every 1.5 s instead.
 *
 * The spec accepts CRLF, LF or a lone CR as a line ending, so all three are
 * folded to LF before splitting. A CR at the very end of the buffer is held
 * back: it may be the first half of a CRLF the next chunk completes, and
 * reading it as a line ending on its own would invent a blank line — an event
 * boundary in the middle of an event.
 */

export function takeEvents(buffer: string): { data: string[]; rest: string } {
  const held = buffer.endsWith("\r") ? "\r" : "";
  const text = (held ? buffer.slice(0, -1) : buffer).replace(/\r\n?/g, "\n");
  const blocks = text.split("\n\n");
  // The trailing fragment is kept for the next chunk rather than parsed
  // half-formed.
  const rest = (blocks.pop() ?? "") + held;
  const data: string[] = [];
  for (const block of blocks) {
    // Several data lines in one event are one value joined by newlines; a
    // comment (": ping") or an event with no data carries nothing to read.
    const lines = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""));
    if (lines.length) data.push(lines.join("\n"));
  }
  return { data, rest };
}
