/**
 * Watching a job to completion.
 *
 * Uses the server-sent stream when it can and falls back to polling when it
 * cannot. EventSource is not usable here: it cannot send an Authorization
 * header, and putting a bearer token in a query string writes it into every
 * proxy and access log along the way.
 *
 * There is deliberately no timeout on the stream. A single LLM call regularly
 * runs for minutes without producing an intermediate update, and a normal
 * receive timeout would kill a connection whose server is working fine —
 * surfacing to the user as "the server took too long" when nothing is wrong.
 */

import { API_BASE, api, getToken } from "./api";
import type { Job } from "./types";

const POLL_MS = 1500;
// How long to give the stream to produce anything at all. A proxy that
// buffers server-sent events holds the connection open and delivers nothing,
// which is indistinguishable from a slow job until it finally gives up
// minutes later — and the panel reads "reading job state" for all of it.
const FIRST_EVENT_MS = 8000;
const TERMINAL = new Set(["done", "failed", "canceled"]);

export function isTerminal(job: Job | null): boolean {
  return !!job && TERMINAL.has(job.state);
}

export interface JobWatch {
  stop: () => void;
}

export function watchJob(
  jobId: string,
  onUpdate: (job: Job) => void,
  onError?: (message: string) => void,
): JobWatch {
  const controller = new AbortController();
  let stopped = false;

  const stop = () => {
    stopped = true;
    controller.abort();
  };

  void (async () => {
    let last: Job | null = null;
    const record = (job: Job) => {
      last = job;
      onUpdate(job);
    };

    // Streaming is an optimisation, not a requirement — a proxy that buffers
    // SSE, or a network that drops the connection, must not stop the user
    // from seeing their job finish. Ending normally is the case that used to
    // be handled backwards: the stream closing without the job having
    // finished meant the watch returned and nothing ever polled, so the panel
    // sat on "reading job state" while the job ran to completion behind it.
    const silent = setTimeout(() => {
      if (!last) controller.abort();
    }, FIRST_EVENT_MS);
    try {
      await streamJob(jobId, controller.signal, record);
    } catch {
      /* fall through to polling */
    } finally {
      clearTimeout(silent);
    }

    if (stopped || isTerminal(last)) return;
    await pollJob(jobId, () => stopped, onUpdate, onError);
  })();

  return { stop };
}

async function streamJob(
  jobId: string,
  signal: AbortSignal,
  onUpdate: (job: Job) => void,
): Promise<void> {
  const token = getToken();
  const response = await fetch(`${API_BASE}/jobs/${jobId}/events`, {
    headers: token ? { Authorization: `Bearer ${token}`, Accept: "text/event-stream" } : {},
    signal,
  });
  if (!response.ok || !response.body) throw new Error("stream unavailable");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });

    // Events are separated by a blank line; the trailing fragment is kept
    // for the next chunk rather than parsed half-formed.
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      try {
        const job = JSON.parse(line.slice(5).trim()) as Job;
        onUpdate(job);
        if (TERMINAL.has(job.state)) return;
      } catch {
        /* a malformed frame is skipped, not fatal */
      }
    }
  }
}

async function pollJob(
  jobId: string,
  stopped: () => boolean,
  onUpdate: (job: Job) => void,
  onError?: (message: string) => void,
): Promise<void> {
  while (!stopped()) {
    try {
      const job = await api.getJob(jobId);
      onUpdate(job);
      if (TERMINAL.has(job.state)) return;
    } catch (error) {
      onError?.(error instanceof Error ? error.message : "Ажлын төлөв уншиж чадсангүй.");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

/** Mongolian labels for the pipeline stages a user actually sees. */
export const JOB_LABELS: Record<string, string> = {
  import_video: "Видео бэлтгэх",
  transcribe: "Яриаг текст болгох",
  suggest: "Санал боловсруулах",
  export_all: "Бүх саналыг экспортлох",
  export: "Экспортлох",
};

export const STAGE_LABELS: Record<string, string> = {
  starting: "Эхэлж байна",
  download: "Файл татаж байна",
  probe: "Мэдээлэл уншиж байна",
  thumbnail: "Хальс бэлдэж байна",
  extract_audio: "Дуу салгаж байна",
  transcribing: "Яриаг таньж байна",
  requesting: "Загвараас хариу хүлээж байна",
  normalize: "Хэсгүүдийг бэлдэж байна",
  join: "Хэсгүүдийг холбож байна",
  subtitles: "Хадмал шатааж байна",
  rendering: "Дүрслэл боловсруулж байна",
  upload: "Үр дүнг хадгалж байна",
  finalize: "Дуусгаж байна",
  done: "Дууссан",
};
