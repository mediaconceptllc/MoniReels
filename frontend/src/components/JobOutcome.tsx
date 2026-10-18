"use client";

/**
 * How a job ended, kept on the page until the producer is done with it.
 *
 * The outcome used to live inside the progress panel, which the page unmounts
 * the moment the job settles — so «амжилтгүй боллоо» and «дууслаа» were never
 * on screen long enough to read. The page simply went back to its button, and
 * a failure looked exactly like nothing having happened. This sits under the
 * rail until it is dismissed or the next job starts.
 */

import { JOB_LABELS } from "@/lib/jobs";
import { explainJobError } from "@/lib/job-errors";
import type { Job } from "@/lib/types";
import { Button, TAP } from "@/components/ui";

export function JobOutcome({
  job,
  retry,
  onDismiss,
}: {
  job: Job;
  /** The way to run this step again, when there is one from this page. */
  retry?: { label: string; onRun: () => void; disabled?: boolean };
  onDismiss: () => void;
}) {
  const label = JOB_LABELS[job.kind] ?? job.kind;
  const explained = job.state === "failed" ? explainJobError(job.error) : null;
  const tone =
    job.state === "done"
      ? "border-fit/40 bg-fit-soft text-fit"
      : job.state === "canceled"
        ? "border-warn/40 bg-warn-soft text-warn"
        : "border-tally/40 bg-tally-soft text-tally";

  return (
    <div
      role={job.state === "failed" ? "alert" : "status"}
      className={`flex flex-col gap-2 rounded-md border px-3.5 py-3 text-sm ${tone}`}
    >
      <p className="font-medium">
        {job.state === "done" && `✓ ${label} дууслаа.`}
        {job.state === "canceled" && `${label} цуцлагдлаа.`}
        {job.state === "failed" && `${label} амжилтгүй боллоо.`}
      </p>
      {explained && <p>{explained.message}</p>}
      {job.state === "failed" && job.error && (
        <details className="text-xs">
          <summary className={`${TAP} inline-flex cursor-pointer select-none items-center`}>
            Техникийн дэлгэрэнгүй
          </summary>
          <p className="mt-1 font-mono [overflow-wrap:anywhere]">{job.error}</p>
        </details>
      )}
      <div className="flex flex-wrap gap-2">
        {retry && (!explained || explained.retry) && (
          <Button tone="primary" disabled={retry.disabled} onClick={retry.onRun}>
            {retry.label}
          </Button>
        )}
        <Button onClick={onDismiss}>Хаах</Button>
      </div>
    </div>
  );
}
