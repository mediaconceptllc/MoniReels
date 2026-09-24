"use client";

/**
 * The pipeline, drawn once.
 *
 * It used to be drawn twice: a card of three buttons at the top, and a strip
 * of four tabs below it. The same four stages, in two shapes, and neither
 * answered the question a producer actually has — where am I, and what
 * happens next. The buttons said what could be started, the tabs said what
 * could be read, and the reason a button was disabled was a separate sentence
 * sitting beside it.
 *
 * One rail now carries all three: the state of every stage, the panel it
 * opens, and — inside the stage that is current — the action that advances
 * it. A stage that cannot run yet says why in its own cell, where the reason
 * belongs, rather than as a footnote under three buttons.
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { usd } from "@/lib/format";
import { Button, Loading, Skeleton } from "@/components/ui";

export type Stage = "source" | "transcript" | "suggestions" | "outputs";

export interface StageDef {
  key: Stage;
  label: string;
  /** Done: it produced something. Current: it is the next real move.
   *  Blocked: an earlier stage has to finish first. */
  state: "done" | "current" | "blocked";
  /** What this stage holds, in the producer's terms — "312 мөр", not "ok". */
  detail: string;
}

export interface RailAction {
  label: string;
  /** Said BEFORE the click, not discovered after: what it will do, how long
   *  it takes, and whether it costs money. */
  note?: string;
  /** What it is likely to charge, with the number of past runs that figure
   *  was measured from. Absent when nothing has been measured yet — a made-up
   *  number beside a paid button is worse than no number at all. */
  cost?: { usd: number; samples: number };
  onRun: () => void;
  disabled?: boolean;
  loading?: boolean;
}

/** The estimate and the evidence for it, together.
 *
 *  The sample count is not a footnote: "≈ $0.03 from one run" and the same
 *  figure from twenty are different claims, and a screen that prints only the
 *  dollars says they are the same. */
function CostHint({ cost }: { cost: NonNullable<RailAction["cost"]> }) {
  return (
    <span className="flex flex-col leading-tight">
      <span className="tabular text-sm font-medium text-ink">≈ {usd(cost.usd)}</span>
      <span className="text-[11px] text-ink-3">
        өмнөх {cost.samples} гүйлтийн хэмжилтээр
      </span>
    </span>
  );
}

function Marker({ state, index }: { state: StageDef["state"]; index: number }) {
  if (state === "done") {
    return (
      <span
        aria-hidden
        className="inline-flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full bg-fit text-surface"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
          <path d="M20 6 9 17l-5-5" />
        </svg>
      </span>
    );
  }
  const shell =
    state === "current"
      ? "bg-accent text-paper"
      : "border border-dashed border-rule text-ink-3";
  return (
    <span
      aria-hidden
      className={`inline-flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full text-xs font-semibold ${shell}`}
    >
      {index + 1}
    </span>
  );
}

/** The action, drawn once so the pinned copy cannot drift from the one in the
 *  rail. The note is the cost stated BEFORE the click, which is why it travels
 *  with the button rather than being left behind. */
function ActionBar({ action }: { action: RailAction }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <Button
        tone="primary"
        className="text-[15px]"
        disabled={action.disabled}
        loading={action.loading}
        onClick={action.onRun}
      >
        {action.label}
      </Button>
      {action.cost && !action.disabled && <CostHint cost={action.cost} />}
      {action.note && (
        <p className="min-w-[220px] flex-1 text-[13px] text-ink-2">{action.note}</p>
      )}
    </div>
  );
}

export function PipelineRail({
  stages,
  active,
  onSelect,
  action,
  children,
}: {
  stages: StageDef[];
  active: Stage;
  onSelect: (stage: Stage) => void;
  /** Absent while a job is running — `children` carries the progress instead. */
  action?: RailAction;
  children?: ReactNode;
}) {
  // On a phone the rail is the first thing on the page and the panel under it
  // is long — three hundred transcript lines, or a grid of six ideas. By the
  // time the producer has read enough to decide, the button that acts on the
  // decision has scrolled away, and the way back to it is to scroll up, act,
  // and lose their place. So once it leaves the screen a compact copy pins
  // itself to the bottom. Both read the same `action`: one button's worth of
  // state, drawn in whichever place is currently visible.
  const barRef = useRef<HTMLDivElement>(null);
  const [offscreen, setOffscreen] = useState(false);
  // What the observer must be rebuilt for is the bar appearing or
  // disappearing, not the label on it changing.
  const hasAction = !!action;
  const hasProgress = !!children;

  useEffect(() => {
    const bar = barRef.current;
    if (!bar) {
      setOffscreen(false);
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => setOffscreen(!entry.isIntersecting),
      { threshold: 0 },
    );
    observer.observe(bar);
    return () => observer.disconnect();
  }, [hasAction, hasProgress]);

  return (
    <div className="overflow-hidden rounded-lg border border-rule bg-surface">
      <div className="grid grid-cols-2 sm:grid-cols-4">
        {stages.map((stage, index) => {
          const selected = stage.key === active;
          return (
            <button
              key={stage.key}
              type="button"
              onClick={() => onSelect(stage.key)}
              aria-current={selected ? "step" : undefined}
              className={`flex min-h-[68px] flex-col gap-1.5 border-b border-rule-soft px-4 py-3 text-left transition-colors sm:border-b-0 sm:border-r sm:last:border-r-0 ${
                selected ? "bg-accent-soft" : "hover:bg-surface-2"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <Marker state={stage.state} index={index} />
                <span
                  className={`text-sm ${
                    stage.state === "blocked" ? "text-ink-3" : "text-ink"
                  } ${selected ? "font-semibold" : "font-medium"}`}
                >
                  {stage.label}
                </span>
              </span>
              <span
                className={`tabular pl-[31px] text-xs ${
                  stage.state === "current" ? "text-accent" : "text-ink-3"
                }`}
              >
                {stage.detail}
              </span>
            </button>
          );
        })}
      </div>

      {(action || children) && (
        <div ref={barRef} className="border-t border-rule p-4">
          {children ?? <ActionBar action={action!} />}
        </div>
      )}

      {/* Fixed, not sticky: sticky is bounded by its scrolling ancestor, so a
          bar inside this box would ride away with the box it belongs to —
          which is the whole thing being prevented. A fixed child escapes the
          rail's `overflow-hidden` because it is positioned against the
          viewport rather than against any ancestor here. */}
      {offscreen && action && !children && (
        <div className="fixed inset-x-0 bottom-0 z-30 flex items-center gap-3 border-t border-rule bg-surface px-4 py-3 sm:hidden">
          <Button
            tone="primary"
            className="flex-1 text-[15px]"
            disabled={action.disabled}
            loading={action.loading}
            onClick={action.onRun}
          >
            {action.label}
          </Button>
          {/* The price travels with the button. A pinned copy without it
              would be the one click in the app that costs money silently. */}
          {action.cost && !action.disabled && <CostHint cost={action.cost} />}
        </div>
      )}
    </div>
  );
}

/**
 * The rail's own geometry, held empty.
 *
 * A spinner would say "wait" and take the layout with it: the page would then
 * arrive as a jump rather than as a fill, and the reader would have to find
 * their place in it twice. These are the same four cells at the same
 * `min-h-[68px]`, the same 22px marker and the same 31px text indent, so
 * nothing moves when the real ones replace them.
 */
export function PipelineRailSkeleton() {
  return (
    <Loading className="overflow-hidden rounded-lg border border-rule bg-surface">
      <div className="grid grid-cols-2 sm:grid-cols-4">
        {[0, 1, 2, 3].map((cell) => (
          <div
            key={cell}
            className="flex min-h-[68px] flex-col gap-2 border-b border-rule-soft px-4 py-3 sm:border-b-0 sm:border-r sm:last:border-r-0"
          >
            <span className="flex items-center gap-2.5">
              <Skeleton className="h-[22px] w-[22px] shrink-0 rounded-full" />
              <Skeleton className="h-3.5 w-20" />
            </span>
            <Skeleton className="ml-[31px] h-3 w-16" />
          </div>
        ))}
      </div>
      <div className="border-t border-rule p-4">
        <Skeleton className="h-11 w-40 rounded-md" />
      </div>
    </Loading>
  );
}
