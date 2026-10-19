"use client";

/** Live progress for one job. Watches until it settles, then reports the
 *  outcome once — a finished job must not keep a stream open. */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { JOB_LABELS, STAGE_LABELS, isTerminal, watchJob } from "@/lib/jobs";
import type { Job } from "@/lib/types";
import { Alert, Button, ProgressBar } from "@/components/ui";
import { ConfirmDialog } from "@/components/ConfirmDialog";

/** Where a waiting job stands, in words. "Ажлын дараалалд хүлээж байна" alone
 *  was the whole answer while another project's two-hour render held the
 *  only slot — and from in here that looks exactly like a stuck job. */
export function queueLine(ahead: number | null | undefined): string {
  if (ahead === undefined || ahead === null) return "Ажлын дараалалд хүлээж байна";
  if (ahead === 0) return "Дараалалд эхнийх — ажиллаж буй ажил дуусмагц эхэлнэ";
  return `Дараалалд таны өмнө ${ahead} ажил байна`;
}

export function JobProgress({
  jobId,
  onSettled,
}: {
  jobId: string;
  onSettled?: (job: Job) => void;
}) {
  const [job, setJob] = useState<Job | null>(null);
  const [watchError, setWatchError] = useState<string | null>(null);
  // Cancelling is asked first and then SEEN to happen. It used to be one
  // unconfirmed tap that changed nothing on screen: a running job only stops
  // at its next step, so 3.5 s later the panel still read "42% … Цуцлах" and
  // the natural move was to press it again.
  const [asking, setAsking] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

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

  async function cancel() {
    setAsking(false);
    setCanceling(true);
    setCancelError(null);
    try {
      const answer = await api.cancelJob(jobId);
      // Already finished between the click and the request: the stream
      // brings the real end, and "cancelling…" would be a false state.
      if (!answer.canceled) setCanceling(false);
    } catch (err) {
      setCanceling(false);
      setCancelError(errorMessage(err));
    }
  }

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
      <div role="status" className="flex items-center gap-2 text-sm text-fit">
        <span aria-hidden>✓</span>
        <span>{label} дууслаа.</span>
      </div>
    );
  }

  // The map first, the worker's own message second. It is the other way
  // round that shipped: `job.message` is written in English for the logs
  // ("Transcribing speech"), and preferring it put English in front of the
  // one user this product has while a complete Mongolian translation of
  // every stage sat unused two files away.
  const stage = STAGE_LABELS[job.stage] ?? job.message ?? job.stage;
  const queued = job.state === "queued";
  const line = canceling
    ? "Цуцалж байна — одоогийн алхам дуусмагц зогсоно"
    : queued
      ? queueLine(job.queue_ahead)
      : stage;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="font-medium text-ink">{label}</span>
        <span className="tabular text-ink-3">
          {queued ? "Дараалалд" : `${Math.round(job.progress * 100)}%`}
        </span>
      </div>
      <ProgressBar value={queued ? 0 : job.progress} label={label} />
      <div className="flex items-center justify-between gap-3">
        {/* Announced when the STEP changes, not on every percent: the bar
            carries the number, and a live region reading "41%, 42%, 43%"
            would drown out everything else on the page. */}
        <span aria-live="polite" className="text-xs text-ink-3">
          {line}
        </span>
        <Button
          tone="quiet"
          loading={canceling}
          disabled={canceling}
          onClick={() => setAsking(true)}
        >
          {canceling ? "Цуцалж байна…" : "Цуцлах"}
        </Button>
      </div>
      {cancelError && <Alert>{cancelError}</Alert>}

      {asking && (
        <ConfirmDialog
          title={`«${label}»-ийг цуцлах уу?`}
          confirmLabel="Цуцлах"
          undo="Дараа нь дахин эхлүүлж болно."
          onConfirm={() => void cancel()}
          onCancel={() => setAsking(false)}
        >
          <p className="text-sm text-ink-2">
            {queued
              ? "Ажил хараахан эхлээгүй — дараалалаас хасагдана, төлбөр гарахгүй."
              : "Одоо явж буй алхам дуусмагц зогсоно. Тэр алхмын төлбөр гарсан хэвээр."}
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
