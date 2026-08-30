"use client";

/** Live progress for one job. Watches until it settles, then reports the
 *  outcome once — a finished job must not keep a stream open. */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { JOB_LABELS, STAGE_LABELS, isTerminal, watchJob } from "@/lib/jobs";
import type { Job } from "@/lib/types";
import { Alert, Button, ProgressBar } from "@/components/ui";

export function JobProgress({
  jobId,
  onSettled,
}: {
  jobId: string;
  onSettled?: (job: Job) => void;
}) {
  const [job, setJob] = useState<Job | null>(null);
  const [watchError, setWatchError] = useState<string | null>(null);

  useEffect(() => {
    let settled = false;
    const watch = watchJob(
      jobId,
      (next) => {
        setJob(next);
        if (!settled && isTerminal(next)) {
          settled = true;
          onSettled?.(next);
        }
      },
      setWatchError,
    );
    return () => watch.stop();
    // onSettled is intentionally excluded: a parent that re-creates the
    // callback each render would otherwise tear down and restart the stream
    // on every tick, which is exactly the update this component causes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  if (watchError) return <Alert tone="warn">{watchError}</Alert>;
  if (!job) return <p className="text-sm text-ink-3">Ажлын төлөв уншиж байна…</p>;

  const label = JOB_LABELS[job.kind] ?? job.kind;

  if (job.state === "failed") {
    return (
      <Alert>
        <span className="font-medium">{label} амжилтгүй боллоо.</span>
        {job.error && <span className="mt-1 block font-mono text-xs">{job.error}</span>}
      </Alert>
    );
  }

  if (job.state === "canceled") {
    return <Alert tone="warn">{label} цуцлагдлаа.</Alert>;
  }

  if (job.state === "done") {
    return (
      <div className="flex items-center gap-2 text-sm text-fit">
        <span aria-hidden>✓</span>
        <span>{label} дууслаа.</span>
      </div>
    );
  }

  const stage = STAGE_LABELS[job.stage] ?? job.stage;
  const queued = job.state === "queued";

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="font-medium text-ink">{label}</span>
        <span className="tabular text-ink-3">
          {queued ? "Дараалалд" : `${Math.round(job.progress * 100)}%`}
        </span>
      </div>
      <ProgressBar value={queued ? 0 : job.progress} />
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-ink-3">
          {queued ? "Ажлын дараалалд хүлээж байна" : (job.message || stage)}
        </span>
        <Button
          tone="quiet"
          className="px-2 py-1 text-xs"
          onClick={() => void api.cancelJob(jobId).catch(() => undefined)}
        >
          Цуцлах
        </Button>
      </div>
    </div>
  );
}
