"use client";

/**
 * Playing a suggestion before rendering it.
 *
 * A reel is 3-5 SEPARATE, non-contiguous spans of the source assembled in
 * order. Reading four timecodes off a card tells a producer almost nothing
 * about whether the result holds together — and the only way to find out was
 * to render all six ideas and watch them.
 *
 * So the cuts are played here, in sequence, from the source the project
 * already has: seek to the first, and when it reaches that cut's end, seek to
 * the next. Nothing is rendered and nothing is uploaded; this is the same
 * video element the source tab uses, driven by a list.
 */

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useDialogFocus } from "@/lib/dialog";
import { timecode } from "@/lib/format";
import { Button } from "@/components/ui";

export interface PreviewCut {
  start: number;
  end: number;
  label: string;
}

export function CutPreview({
  sourceUrl,
  cuts,
  title,
  onClose,
}: {
  sourceUrl: string;
  cuts: PreviewCut[];
  title: string;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [index, setIndex] = useState(0);
  const [done, setDone] = useState(false);

  const seekTo = useCallback(
    (next: number) => {
      const video = videoRef.current;
      const cut = cuts[next];
      if (!video || !cut) return;
      video.currentTime = cut.start;
      void video.play().catch(() => undefined);
    },
    [cuts],
  );

  useEffect(() => {
    seekTo(0);
    // Only on mount: re-seeking whenever `cuts` is re-created would restart
    // the preview on every parent render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Esc closes, because this covers the page. Focus moves in on open — it
  // stayed on the card underneath — and returns to «Урьдчилж үзэх» after.
  const boxRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  useDialogFocus(boxRef, { onClose, initial: closeRef });

  function onTime() {
    const video = videoRef.current;
    const cut = cuts[index];
    if (!video || !cut || done) return;
    if (video.currentTime < cut.end) return;
    const next = index + 1;
    if (next >= cuts.length) {
      video.pause();
      setDone(true);
      return;
    }
    setIndex(next);
    seekTo(next);
  }

  const elapsed = cuts
    .slice(0, index)
    .reduce((sum, cut) => sum + Math.max(0, cut.end - cut.start), 0);
  const total = cuts.reduce((sum, cut) => sum + Math.max(0, cut.end - cut.start), 0);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-2 sm:p-4"
      onClick={onClose}
    >
      {/* Bounded by the screen, scrolling inside it. MEASURED at 844×390 (a
          phone on its side): the box was 623px tall in a 390px viewport and
          started at y = −116, so its title and «Хаах» were above the top
          edge with no way to scroll to them. */}
      <div
        ref={boxRef}
        className="flex max-h-[calc(100dvh-1rem)] w-full max-w-3xl flex-col gap-3 overflow-y-auto rounded-lg border border-rule bg-surface p-4 sm:max-h-[calc(100dvh-2rem)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p id={titleId} className="font-display text-[15px] font-medium leading-snug text-ink">
              {title}
            </p>
            <p className="tabular mt-0.5 text-xs text-ink-3">
              {cuts.length} огтлол · нийт {Math.round(total)} сек
            </p>
          </div>
          <Button ref={closeRef} tone="quiet" onClick={onClose}>
            Хаах
          </Button>
        </div>

        {/* Height-bounded too: the cut list under it is what the producer
            is judging, and a full-width 16:9 frame on a landscape phone is
            taller than the screen on its own. */}
        <video
          ref={videoRef}
          src={sourceUrl}
          controls
          playsInline
          onTimeUpdate={onTime}
          className="max-h-[55dvh] w-full rounded bg-black object-contain"
        />

        {/* Which cut is playing, and where it sits in the source. Both matter:
            the producer is judging the assembly, not one clip. */}
        <ol className="flex flex-col gap-1">
          {cuts.map((cut, i) => (
            <li
              key={`${cut.start}-${i}`}
              className={`flex items-baseline gap-2.5 rounded px-2 py-1 text-xs ${
                i === index && !done ? "bg-accent-soft text-accent" : "text-ink-3"
              }`}
            >
              <span className="w-16 shrink-0">{cut.label}</span>
              <span className="tabular font-mono">
                {timecode(cut.start)}–{timecode(cut.end)}
              </span>
              <span className="tabular ml-auto">{Math.round(cut.end - cut.start)} сек</span>
            </li>
          ))}
        </ol>

        <p className="tabular text-xs text-ink-3">
          {done
            ? "Дууслаа."
            : `${index + 1} / ${cuts.length} огтлол · ${Math.round(elapsed)} сек өнгөрлөө`}
        </p>
      </div>
    </div>
  );
}
