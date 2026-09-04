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

import type { ReactNode } from "react";
import { Button, Spinner } from "@/components/ui";

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
  onRun: () => void;
  disabled?: boolean;
  loading?: boolean;
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
        <div className="border-t border-rule p-4">
          {children ?? (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <Button
                tone="primary"
                className="min-h-[44px] px-4 text-[15px]"
                disabled={action!.disabled}
                loading={action!.loading}
                onClick={action!.onRun}
              >
                {action!.label}
              </Button>
              {action!.note && (
                <p className="min-w-[220px] flex-1 text-[13px] text-ink-2">{action!.note}</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The rail's own placeholder, so the page keeps its shape while a project
 *  loads instead of collapsing to a spinner on a blank page. */
export function PipelineRailSkeleton() {
  return (
    <div className="flex min-h-[68px] items-center justify-center rounded-lg border border-rule bg-surface">
      <Spinner />
    </div>
  );
}
