/**
 * The browser half of the multipart upload, checked against fakes of R2 and
 * the API (audit UP-1).
 *
 * The backend tests prove the server signs, checks and assembles correctly;
 * none of them can see what the browser does with a part that fails, an URL
 * that expires, a page that reloads halfway, or a server that predates
 * multipart. A 6 GiB file is simulated by its part arithmetic — no byte of it
 * exists — so this runs in a second.
 *
 * Run: npm run check-upload
 */

const MIB = 1024 ** 2;
const GIB = 1024 ** 3;

// ---------------------------------------------------------------- browser --
const store = new Map<string, string>();
(globalThis as Record<string, unknown>).window = {
  localStorage: {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  },
  dispatchEvent: () => true,
  location: { origin: "http://localhost:3000" },
};
// Retries back off for seconds; here time does not pass.
(globalThis as Record<string, unknown>).setTimeout = (fn: () => void) => {
  setImmediate(fn);
  return 0;
};

/** What R2 holds, and what it is told to do to the next requests. */
const r2 = {
  parts: new Map<string, Map<number, number>>(), // upload id → part → bytes
  singlePuts: 0,
  putsByPart: new Map<number, number>(),
  /** part → how many more times it fails as a dropped connection */
  drop: new Map<number, number>(),
  /** parts whose URL answers 403 once (expired) */
  expire: new Set<number>(),
  /** stop after this many successful part PUTs (a page closed mid-upload) */
  stopAfter: Infinity,
  reset() {
    this.singlePuts = 0;
    this.putsByPart.clear();
    this.drop.clear();
    this.expire.clear();
    this.stopAfter = Infinity;
  },
};

class FakeXHR {
  status = 0;
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number }) => void) | null } = {
    onprogress: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  private url = "";
  private aborted = false;
  open(_method: string, url: string) {
    this.url = url;
  }
  setRequestHeader() {}
  abort() {
    this.aborted = true;
    setImmediate(() => this.onabort?.());
  }
  send(body: { size: number }) {
    setImmediate(() => {
      if (this.aborted) return;
      const url = new URL(this.url);
      const part = Number(url.searchParams.get("partNumber"));
      if (!part) {
        r2.singlePuts++;
        this.status = 200;
        this.onload?.();
        return;
      }
      if (r2.expire.delete(part)) {
        this.status = 403;
        this.onload?.();
        return;
      }
      const left = r2.drop.get(part) ?? 0;
      if (left > 0) {
        r2.drop.set(part, left - 1);
        this.onerror?.();
        return;
      }
      if (r2.stopAfter <= 0) return; // the tab is gone: nothing ever answers
      r2.stopAfter--;
      this.upload.onprogress?.({ lengthComputable: true, loaded: body.size });
      r2.putsByPart.set(part, (r2.putsByPart.get(part) ?? 0) + 1);
      r2.parts.get(url.searchParams.get("uploadId")!)!.set(part, body.size);
      this.status = 200;
      this.onload?.();
    });
  }
}
(globalThis as Record<string, unknown>).XMLHttpRequest = FakeXHR;

// --------------------------------------------------------------------- API --
/** The server's plan, as app.r2.part_plan states it — the fixture plays the
 *  server, so it answers with the server's numbers. */
function plan(size: number): [number, number] {
  const perPart = Math.ceil(size / 10000);
  const partSize = Math.max(16 * MIB, Math.ceil(perPart / MIB) * MIB);
  return [partSize, Math.ceil(size / partSize)];
}

const api = {
  declared: 0,
  multipart: true, // false: a server from before multipart (404)
  calls: [] as string[],
  signed: [] as number[][],
  uploads: 0,
};

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const path = new URL(String(input), "http://api.test").pathname;
  const body = init?.body ? JSON.parse(String(init.body)) : {};
  const route = path.replace(/^\/projects\/p1/, "");
  api.calls.push(route);
  const json = (value: unknown, status = 200) =>
    new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  const [partSize, partCount] = plan(api.declared);
  switch (route) {
    case "/upload/multipart": {
      if (!api.multipart) return json({ detail: "Not Found" }, 404);
      if (api.declared < 64 * MIB) return json({ mode: "single" });
      const id = `u${++api.uploads}`;
      r2.parts.set(id, new Map());
      return json({ mode: "multipart", upload_id: id, part_size: partSize, part_count: partCount, sign_batch: 50 });
    }
    case "/upload/multipart/sign":
      api.signed.push(body.part_numbers);
      return json({
        urls: Object.fromEntries(
          body.part_numbers.map((n: number) => [String(n), `https://r2.test/k?uploadId=${body.upload_id}&partNumber=${n}`]),
        ),
        expires_in_s: 3600,
      });
    case "/upload/multipart/status": {
      const held = r2.parts.get(body.upload_id);
      if (!held) return json({ detail: "gone" }, 404);
      return json({
        part_size: partSize,
        part_count: partCount,
        sign_batch: 50,
        parts: [...held].map(([part_number, size]) => ({ part_number, size })),
      });
    }
    case "/upload/multipart/complete": {
      const held = r2.parts.get(body.upload_id)!;
      const total = [...held.values()].reduce((a, b) => a + b, 0);
      if (held.size !== partCount || total !== api.declared) return json({ detail: "дутуу" }, 400);
      return json({ completed: true, parts: partCount });
    }
    case "/upload/multipart/abort":
      r2.parts.delete(body.upload_id);
      return json({ aborted: true });
    default:
      return json({ detail: `unexpected ${route}` }, 500);
  }
}) as typeof fetch;

// -------------------------------------------------------------------- file --
function fakeFile(size: number, name = "film.mp4", lastModified = 1): File {
  return {
    name,
    size,
    lastModified,
    type: "video/mp4",
    slice: (start: number, end: number) => ({ size: Math.min(end, size) - start }),
  } as unknown as File;
}

const problems: string[] = [];
function expect(name: string, ok: boolean, extra = "") {
  if (!ok) problems.push(`${name}${extra ? ` — ${extra}` : ""}`);
}

async function outcome(promise: Promise<void>): Promise<string> {
  try {
    await promise;
    return "ok";
  } catch (err) {
    return (err as Error).message;
  }
}

async function main() {
  const { uploadSource, savedUpload } = await import("../src/lib/upload");
  const single = { asked: 0 };
  const singleUrl = async () => {
    single.asked++;
    return "https://r2.test/k?single=1";
  };

  // 1. A 6 GiB file — past one PUT's ~5 GiB — goes in parts, a dropped part
  //    and an expired URL are retried alone, and the server completes it.
  api.declared = 6 * GIB + 12345;
  const [, count] = plan(api.declared);
  r2.reset();
  r2.drop.set(7, 2);
  r2.expire.add(40);
  const seen: number[] = [];
  let result = await outcome(uploadSource("p1", fakeFile(api.declared), singleUrl, (f) => seen.push(f)).promise);
  expect("6 GiB: completes", result === "ok", result);
  expect("6 GiB: in parts", count === 385 && r2.singlePuts === 0, `${count} parts, ${r2.singlePuts} single`);
  expect("6 GiB: every part stored once", [...r2.putsByPart.values()].every((n) => n === 1) && r2.putsByPart.size === count);
  expect("6 GiB: completed", api.calls.includes("/upload/multipart/complete"));
  expect("6 GiB: signed in batches of ≤ 50", api.signed.every((b) => b.length <= 50));
  expect("6 GiB: the expired part was signed again", api.signed.filter((b) => b.includes(40)).length === 2);
  expect("6 GiB: progress ends at 100%", seen[seen.length - 1] === 1, String(seen[seen.length - 1]));
  expect("6 GiB: nothing left to resume", savedUpload("p1") === null);

  // 2. A part that keeps failing pauses the upload with a way on — and the
  //    next attempt with the same file sends only what R2 lacks.
  api.calls.length = 0;
  api.signed.length = 0;
  r2.reset();
  api.declared = 200 * MIB; // 13 parts
  r2.drop.set(9, 99);
  result = await outcome(uploadSource("p1", fakeFile(api.declared), singleUrl, () => undefined).promise);
  expect("stuck part: pauses", result.includes("Үргэлжлүүлэх"), result);
  expect("stuck part: resumable", savedUpload("p1") !== null);
  const before = new Map(r2.putsByPart);
  r2.drop.clear();
  result = await outcome(uploadSource("p1", fakeFile(api.declared), singleUrl, () => undefined).promise);
  expect("resume: completes", result === "ok", result);
  expect("resume: asked R2 what it holds", api.calls.includes("/upload/multipart/status"));
  expect("resume: did not start another upload", api.calls.filter((c) => c === "/upload/multipart").length === 1);
  const resent = [...before.keys()].filter((p) => (r2.putsByPart.get(p) ?? 0) > 1);
  expect("resume: stored parts not sent again", resent.length === 0, `resent ${resent.join(",")}`);

  // 3. A reload in the middle: a new page, the same file, the rest only.
  api.calls.length = 0;
  r2.reset();
  r2.stopAfter = 5;
  const halted = uploadSource("p1", fakeFile(api.declared, "film.mp4", 7), singleUrl, () => undefined);
  await new Promise((r) => setImmediate(r));
  for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r));
  halted.pause(); // the tab closes
  await outcome(halted.promise);
  expect("reload: the upload id survives", savedUpload("p1")?.lastModified === 7);
  r2.stopAfter = Infinity;
  result = await outcome(uploadSource("p1", fakeFile(api.declared, "film.mp4", 7), singleUrl, () => undefined).promise);
  expect("reload: completes", result === "ok", result);
  expect("reload: 5 parts were already there", [...r2.putsByPart.values()].every((n) => n === 1));

  // 4. A different file of the same name is not appended to the old upload.
  api.calls.length = 0;
  r2.reset();
  r2.drop.set(3, 99);
  await outcome(uploadSource("p1", fakeFile(api.declared, "film.mp4", 1), singleUrl, () => undefined).promise);
  r2.drop.clear();
  api.calls.length = 0;
  result = await outcome(uploadSource("p1", fakeFile(api.declared, "film.mp4", 2), singleUrl, () => undefined).promise);
  expect("other file: completes", result === "ok", result);
  expect("other file: a new upload, not a resume", !api.calls.includes("/upload/multipart/status") && api.calls.includes("/upload/multipart"));

  // 5. Cancel throws the stored parts away.
  api.calls.length = 0;
  r2.reset();
  r2.stopAfter = 3;
  const cancelled = uploadSource("p1", fakeFile(api.declared), singleUrl, () => undefined);
  for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r));
  await cancelled.cancel();
  result = await outcome(cancelled.promise);
  expect("cancel: says it stopped", result === "Хуулалт зогслоо.", result);
  expect("cancel: aborts the parts", api.calls.includes("/upload/multipart/abort"));
  expect("cancel: nothing to resume", savedUpload("p1") === null);

  // 6. A small file, and a server from before multipart: one PUT.
  r2.reset();
  api.declared = 10 * MIB;
  result = await outcome(uploadSource("p1", fakeFile(api.declared), singleUrl, () => undefined).promise);
  expect("small: one PUT", result === "ok" && r2.singlePuts === 1 && single.asked === 1, `${result} ${r2.singlePuts}`);
  r2.reset();
  api.multipart = false;
  api.declared = 300 * MIB;
  result = await outcome(uploadSource("p1", fakeFile(api.declared), singleUrl, () => undefined).promise);
  expect("old server: falls back to one PUT", result === "ok" && r2.singlePuts === 1, `${result} ${r2.singlePuts}`);

  if (problems.length) {
    console.error("check-upload failed:\n  " + problems.join("\n  "));
    process.exit(1);
  }
  console.log(
    "check-upload: 6 GiB in 385 parts, dropped parts and expired URLs retried alone, " +
      "a reload resumes without resending, cancel aborts, small files and old servers use one PUT.",
  );
}

void main();

// A module, not a script: its names would otherwise collide with the other
// checks' in the one program `tsc --noEmit` builds.
export {};
