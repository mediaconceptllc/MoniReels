"use client";

/**
 * Where a project's video is, before it can be shown — and the way forward.
 *
 * The page used to say "Видео боловсруулагдаж байна…" whatever had happened:
 * an import that failed, an upload that was cancelled or never arrived, an
 * import waiting behind a stopped worker. Each of those now says what it is
 * (audit UX-6); the two that need a new video offer one, and a waiting
 * import says in plain words why it is waiting.
 */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { QueueStatus } from "@/lib/types";
import { SourceUpload } from "@/components/SourceUpload";
import { Alert, Card, Empty } from "@/components/ui";

export type SourceState = "ready" | "queued" | "running" | "failed" | "canceled" | "missing";

/** One line for the page header. */
export function sourceSummary(state: SourceState): string {
  switch (state) {
    case "failed":
      return "Видеог боловсруулж чадсангүй";
    case "canceled":
      return "Видеоны боловсруулалт цуцлагдсан";
    case "missing":
      return "Видео хуулагдаагүй байна";
    default:
      return "Видео боловсруулагдаж байна…";
  }
}

/** Why a queued import has not started, in the producer's words. */
function QueueNote() {
  const [queue, setQueue] = useState<QueueStatus | null>(null);
  useEffect(() => {
    let live = true;
    const read = () =>
      api
        .queueStatus()
        .then((q) => live && setQueue(q))
        .catch(() => undefined);
    void read();
    const timer = window.setInterval(read, 15000);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, []);
  if (!queue) return null;
  // An import is a render (the heavy lane): with the render pool down and the
  // I/O pool alive, `stalled` is false and the video still waits for nobody.
  if (queue.stalled || queue.unserved_lanes.includes("heavy")) {
    return (
      <Alert tone="warn">
        Видео боловсруулах сервис одоогоор ажиллахгүй байна. Таны видео дараалалд хадгалагдсан —
        сервис сэргэмэгц өөрөө эхэлнэ. Удаан үргэлжилбэл админд хэлнэ үү.
      </Alert>
    );
  }
  return (
    <p className="text-sm text-ink-2">
      {queue.waiting > 1
        ? `Таны видео дараалалд байна: нийт ${queue.waiting} ажил ээлжээ хүлээж байна. Ээлж ирэхэд өөрөө эхэлнэ.`
        : "Таны видео дараалалд байна. Ээлж ирэхэд өөрөө эхэлнэ."}
    </p>
  );
}

export function SourceStatus({
  projectId,
  state,
  reason,
  onUploaded,
}: {
  projectId: string;
  state: SourceState;
  /** Why the import failed, already in Mongolian (lib/job-errors). */
  reason: string | null;
  onUploaded: () => void;
}) {
  if (state === "queued" || state === "running" || state === "ready") {
    return (
      <div className="flex flex-col gap-3">
        <Empty title="Видео боловсруулагдаж байна" hint="Дууссаны дараа энд харагдана." />
        {state === "queued" && <QueueNote />}
      </div>
    );
  }
  const heading =
    state === "failed"
      ? "Видеог боловсруулж чадсангүй"
      : state === "canceled"
        ? "Видеоны боловсруулалт цуцлагдсан"
        : "Видео хуулагдаагүй байна";
  const hint =
    state === "failed"
      ? reason
      : state === "canceled"
        ? "Видеог дахин хуулж боловсруулалтыг эхлүүлнэ үү."
        : "Хуулалт дуусаагүй, тасарсан эсвэл цуцлагдсан байна. Видеогоо сонгож хуулна уу.";
  return (
    <Card className="flex max-w-3xl flex-col gap-3 p-5">
      <h3 className="font-display text-base font-semibold">{heading}</h3>
      {hint && <p className="text-sm text-ink-2">{hint}</p>}
      <SourceUpload
        projectId={projectId}
        label={state === "failed" ? "Дахин хуулах" : "Видео хуулах"}
        onUploaded={onUploaded}
      />
    </Card>
  );
}
