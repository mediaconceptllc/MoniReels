/**
 * Direct-to-R2 upload.
 *
 * The file goes straight from the browser to object storage on presigned
 * URLs; it never passes through the API. A multi-gigabyte video through a
 * dyno is a request timeout and a doubled bandwidth bill.
 *
 * A large file goes in PARTS (audit UP-1). One presigned PUT cannot carry
 * more than ~5 GiB — R2 refuses it — while the limit is 20 GiB, so a 6 GB
 * video failed after uploading for an hour; and a dropped connection started
 * a 4 GB upload again from the first byte. Now a failed part is retried
 * alone, the parts already stored survive a reload (the upload id is kept in
 * localStorage), and the server assembles the object from R2's own list of
 * parts. Whether to split at all is the SERVER's decision, from the size the
 * file was declared at, so the rule lives in one place.
 *
 * XMLHttpRequest rather than fetch, purely for `upload.onprogress`: fetch has
 * no upload-progress event, and a 4 GB upload with no visible progress is
 * indistinguishable from a hang.
 */

import { api, ApiError } from "@/lib/api";
import { fileSize } from "@/lib/format";
import type { MultipartStart } from "@/lib/types";

export const ACCEPT = ".mp4,.mov,.mkv,.webm,.m4v,.avi";
// The server's own limit (schemas.UPLOAD_MAX_BYTES) — it checks again; this
// only saves a 20 GB upload from being refused at the end.
export const MAX_BYTES = 20 * 1024 ** 3;

/** Why this file will be refused, or null. `accept` narrows the picker but
 *  not "All files" or a drag-and-drop, and the refusal used to arrive from
 *  the server in English ("Value error, Unsupported video format '.txt'…"). */
export function uploadRefusal(file: File): string | null {
  const ext = file.name.includes(".") ? `.${file.name.split(".").pop()!.toLowerCase()}` : "";
  if (!ACCEPT.split(",").includes(ext)) {
    return `Энэ файлын төрөл дэмжигдэхгүй${ext ? ` (${ext})` : ""}. Зөвшөөрөгдөх: ${ACCEPT.split(",").join(", ")}.`;
  }
  if (file.size > MAX_BYTES) {
    return `Файл хэт том байна (${fileSize(file.size)}). Дээд хэмжээ ${fileSize(MAX_BYTES)}.`;
  }
  return null;
}

export interface UploadHandle {
  promise: Promise<void>;
  abort: () => void;
}

/** A storage answer worth telling apart: an expired URL is re-signed, a
 *  dropped connection is retried, anything else is final. */
class StorageError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const STOPPED = "Хуулалт зогслоо.";

function put(
  url: string,
  body: Blob,
  contentType: string | null,
  onProgress: (loaded: number) => void,
  track?: (xhr: XMLHttpRequest | null) => void,
): Promise<void> {
  const xhr = new XMLHttpRequest();
  track?.(xhr);
  return new Promise<void>((resolve, reject) => {
    xhr.open("PUT", url, true);
    // Must match the Content-Type the URL was signed with, or the signature
    // check fails with a 403 that says nothing about why. A part's URL signs
    // none.
    if (contentType) xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded);
    };
    xhr.onload = () => {
      track?.(null);
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(body.size);
        resolve();
      } else if (xhr.status === 403) {
        reject(
          new StorageError(
            403,
            "Хуулах холбоос хүчингүй болсон байна. Хуудсыг сэргээгээд дахин оролдоно уу.",
          ),
        );
      } else {
        reject(new StorageError(xhr.status, `Хадгалах сан файлыг татгалзлаа (${xhr.status}).`));
      }
    };
    xhr.onerror = () => {
      track?.(null);
      reject(new StorageError(0, "Сүлжээний алдаа: файл бүрэн хуулагдсангүй."));
    };
    xhr.onabort = () => {
      track?.(null);
      reject(new StorageError(-1, STOPPED));
    };
    xhr.send(body);
  });
}

/** One presigned PUT of the whole file — small files, and servers that
 *  predate multipart. */
export function uploadToStorage(
  url: string,
  file: File,
  onProgress: (fraction: number) => void,
): UploadHandle {
  let current: XMLHttpRequest | null = null;
  const promise = put(
    url,
    file,
    file.type || "application/octet-stream",
    (loaded) => onProgress(file.size ? loaded / file.size : 1),
    (xhr) => {
      current = xhr;
    },
  );
  return { promise, abort: () => current?.abort() };
}

// ---------------------------------------------------------------------------
// Multipart
// ---------------------------------------------------------------------------

/** Parts in flight at once. Enough to fill an upload link; few enough that
 *  a retried part does not wait behind a crowd. */
const CONCURRENCY = 4;
/** Tries per part before the upload pauses and asks to be resumed. */
const MAX_TRIES = 6;
const RESUME_KEY = "monireels.upload.";

/** What a reloaded page needs to carry on where the last one stopped. */
export interface SavedUpload {
  uploadId: string;
  name: string;
  size: number;
  lastModified: number;
  partSize: number;
  partCount: number;
}

export function savedUpload(projectId: string): SavedUpload | null {
  try {
    const raw = window.localStorage.getItem(RESUME_KEY + projectId);
    return raw ? (JSON.parse(raw) as SavedUpload) : null;
  } catch {
    return null;
  }
}

function remember(projectId: string, saved: SavedUpload | null) {
  try {
    if (saved) window.localStorage.setItem(RESUME_KEY + projectId, JSON.stringify(saved));
    else window.localStorage.removeItem(RESUME_KEY + projectId);
  } catch {
    /* private mode: resuming after a reload is lost, the upload is not */
  }
}

/** Is `file` the one a saved upload was for? Name, size and modification
 *  time — a different file with the same name must not be appended to it. */
export function sameFile(saved: SavedUpload, file: File): boolean {
  return saved.name === file.name && saved.size === file.size && saved.lastModified === file.lastModified;
}

export function partBytes(size: number, partSize: number, partCount: number, n: number): number {
  return n < partCount ? partSize : size - partSize * (partCount - 1);
}

export interface SourceUpload {
  promise: Promise<void>;
  /** Stops the transfer. Stored parts stay, for a later `uploadSource` of
   *  the same file to finish. */
  pause: () => void;
  /** Stops it and throws the stored parts away (they are billed until
   *  aborted, and appear in no listing). */
  cancel: () => Promise<void>;
}

type Plan =
  | { mode: "single" }
  | {
      mode: "multipart";
      uploadId: string;
      partSize: number;
      partCount: number;
      signBatch: number;
      done: Set<number>;
    };

async function planFor(projectId: string, file: File): Promise<Plan> {
  const saved = savedUpload(projectId);
  if (saved && sameFile(saved, file)) {
    try {
      const status = await api.multipartStatus(projectId, saved.uploadId);
      const done = new Set(
        status.parts
          .filter((p) => p.size === partBytes(file.size, status.part_size, status.part_count, p.part_number))
          .map((p) => p.part_number),
      );
      return {
        mode: "multipart",
        uploadId: saved.uploadId,
        partSize: status.part_size,
        partCount: status.part_count,
        signBatch: status.sign_batch,
        done,
      };
    } catch (err) {
      // Gone (expired, aborted) or no longer pending: start again below.
      if (!(err instanceof ApiError) || (err.status !== 404 && err.status !== 409)) throw err;
      remember(projectId, null);
    }
  }
  let started: MultipartStart;
  try {
    started = await api.startMultipart(projectId);
  } catch (err) {
    // A server from before multipart has no such route: the single PUT it
    // already handed out still works, up to its ~5 GiB.
    if (err instanceof ApiError && err.status === 404) return { mode: "single" };
    throw err;
  }
  if (started.mode === "single" || !started.upload_id || !started.part_size || !started.part_count) {
    return { mode: "single" };
  }
  return {
    mode: "multipart",
    uploadId: started.upload_id,
    partSize: started.part_size,
    partCount: started.part_count,
    signBatch: started.sign_batch ?? 1,
    done: new Set(),
  };
}

/**
 * Upload a project's source, in parts when the server says so.
 *
 * `singleUrl` is asked for only when the whole file goes in one PUT: on a
 * first attempt it is the URL the project was created with, on a retry a
 * fresh one (an hour-old URL has expired).
 */
export function uploadSource(
  projectId: string,
  file: File,
  singleUrl: () => Promise<string>,
  onProgress: (fraction: number) => void,
): SourceUpload {
  let stopped = false;
  let uploadId: string | null = null;
  const inFlight = new Set<XMLHttpRequest>();
  let single: UploadHandle | null = null;

  const stop = () => {
    stopped = true;
    single?.abort();
    for (const xhr of inFlight) xhr.abort();
  };

  const promise = (async () => {
    const plan = await planFor(projectId, file);
    if (stopped) throw new Error(STOPPED);
    if (plan.mode === "single") {
      const url = await singleUrl();
      if (stopped) throw new Error(STOPPED);
      single = uploadToStorage(url, file, onProgress);
      await single.promise;
      return;
    }

    uploadId = plan.uploadId;
    remember(projectId, {
      uploadId: plan.uploadId,
      name: file.name,
      size: file.size,
      lastModified: file.lastModified,
      partSize: plan.partSize,
      partCount: plan.partCount,
    });

    const { partSize, partCount, done } = plan;
    const bytes = (n: number) => partBytes(file.size, partSize, partCount, n);
    const loaded = new Map<number, number>();
    for (const n of done) loaded.set(n, bytes(n));
    const report = () => {
      let sum = 0;
      for (const value of loaded.values()) sum += value;
      onProgress(file.size ? Math.min(1, sum / file.size) : 1);
    };
    report();

    const pending: number[] = [];
    for (let n = 1; n <= partCount; n++) if (!done.has(n)) pending.push(n);
    const urls = new Map<number, string>();
    let signing: Promise<void> | null = null;

    // Signed a batch at a time, as the upload reaches them: a 20 GB upload
    // outlives one URL's hour, so signing everything up front would end in
    // expired URLs.
    const urlFor = async (n: number): Promise<string> => {
      while (!urls.has(n)) {
        if (!signing) {
          const batch = [n, ...pending.filter((m) => !urls.has(m) && m !== n)].slice(0, plan.signBatch);
          signing = api
            .signParts(projectId, plan.uploadId, batch)
            .then((signed) => {
              for (const [key, url] of Object.entries(signed.urls)) urls.set(Number(key), url);
            })
            .finally(() => {
              signing = null;
            });
        }
        await signing;
      }
      return urls.get(n)!;
    };

    const sendPart = async (n: number) => {
      const blob = file.slice((n - 1) * partSize, (n - 1) * partSize + bytes(n));
      for (let attempt = 1; ; attempt++) {
        if (stopped) throw new Error(STOPPED);
        const url = await urlFor(n);
        let mine: XMLHttpRequest | null = null;
        try {
          await put(
            url,
            blob,
            null,
            (value) => {
              loaded.set(n, value);
              report();
            },
            (xhr) => {
              if (xhr) inFlight.add((mine = xhr));
              else if (mine) inFlight.delete(mine);
            },
          );
          return;
        } catch (err) {
          loaded.set(n, 0);
          report();
          if (stopped || !(err instanceof StorageError) || err.status === -1) throw new Error(STOPPED);
          if (err.status === 403) {
            urls.delete(n); // expired: signed again on the next try
          } else if (err.status !== 0 && err.status < 500) {
            throw err;
          }
          if (attempt >= MAX_TRIES) {
            throw new Error(
              `Сүлжээ тасарсаар байна: ${n}-р хэсэг ${MAX_TRIES} удаа оролдоод хуулагдсангүй. ` +
                "Хуулагдсан хэсгүүд хадгалагдсан — «Үргэлжлүүлэх»-ийг дарж үлдсэнийг нь хуулна уу.",
            );
          }
          if (err.status !== 403) await new Promise((r) => setTimeout(r, 1000 * 2 ** (attempt - 1)));
        }
      }
    };

    let failure: unknown = null;
    const worker = async () => {
      while (pending.length && !stopped && !failure) {
        const n = pending.shift()!;
        try {
          await sendPart(n);
          done.add(n);
        } catch (err) {
          failure ??= err;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, pending.length) }, worker));
    if (failure) throw failure;
    if (stopped) throw new Error(STOPPED);

    await api.completeMultipart(projectId, plan.uploadId);
    remember(projectId, null);
  })();

  return {
    promise,
    pause: stop,
    cancel: async () => {
      stop();
      remember(projectId, null);
      if (uploadId) await api.abortMultipart(projectId, uploadId).catch(() => undefined);
    },
  };
}
