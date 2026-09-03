"use client";

/**
 * What the model proposed, and what to do about it.
 *
 * A reel is 3-5 SEPARATE, non-contiguous cuts assembled in the order shown —
 * not one continuous excerpt — so each cut is listed with its own role and
 * span, and the total is summed. The total is the number that decides whether
 * a reel is usable (35-60s), so it is shown rather than left to be counted.
 *
 * These cards used to be read-only. Six ideas came back and the only button
 * rendered all six: a producer who wanted the second short encoded a
 * 42-minute source six times and deleted five results. Each card is now
 * selectable and playable, and the export renders exactly what is ticked.
 */

import { useMemo, useState } from "react";
import { CUT_ROLE_LABELS, timecode, totalCutSeconds } from "@/lib/format";
import type { Suggestions } from "@/lib/types";
import { Badge, Button, Card } from "@/components/ui";
import { CutPreview, type PreviewCut } from "@/components/CutPreview";

/** The window a reel has to land in to be usable on any of the platforms
 *  this exports for. Stated once; the badge and the hint both read it. */
const FIT_MIN = 35;
const FIT_MAX = 60;

function Tick({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden
      className={`inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded border-[1.5px] ${
        on ? "border-accent bg-accent" : "border-rule bg-transparent"
      }`}
    >
      {on && (
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--paper)" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M20 6 9 17l-5-5" />
        </svg>
      )}
    </span>
  );
}

export function SuggestionList({
  suggestions,
  sourceUrl,
  onExport,
  busy = false,
}: {
  suggestions: Suggestions;
  /** Signed and short-lived; absent while the import is still running, in
   *  which case there is nothing to play and the button is not drawn. */
  sourceUrl?: string | null;
  /** Absent for a reader who cannot start a render. */
  onExport?: (pick: { shorts: string[]; youtube: number[] }) => void;
  busy?: boolean;
}) {
  const [shorts, setShorts] = useState<string[]>([]);
  const [plans, setPlans] = useState<number[]>([]);
  const [preview, setPreview] = useState<{ title: string; cuts: PreviewCut[] } | null>(null);

  const picked = shorts.length + plans.length;
  const total = suggestions.shorts.length + suggestions.youtube.length;
  const allPicked = picked === total;

  function toggleShort(id: string) {
    setShorts((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }
  function togglePlan(index: number) {
    setPlans((prev) => (prev.includes(index) ? prev.filter((x) => x !== index) : [...prev, index]));
  }
  function toggleAll() {
    setShorts(allPicked ? [] : suggestions.shorts.map((s) => s.id));
    setPlans(allPicked ? [] : suggestions.youtube.map((_, i) => i));
  }

  // Nothing ticked means "render everything", which is what the button did
  // before there was a choice — so the common case still takes one click.
  const exportPick = useMemo(
    () =>
      picked === 0
        ? { shorts: suggestions.shorts.map((s) => s.id), youtube: suggestions.youtube.map((_, i) => i) }
        : { shorts, youtube: plans },
    [picked, shorts, plans, suggestions],
  );

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <button
          type="button"
          onClick={toggleAll}
          className="flex min-h-[44px] items-center gap-2.5 text-sm text-ink-2 hover:text-ink"
        >
          <Tick on={allPicked} />
          {picked === 0 ? "Бүгдийг сонгох" : `${picked} сонгосон`}
        </button>
        {onExport && (
          <Button
            tone="primary"
            className="min-h-[44px] px-4"
            loading={busy}
            onClick={() => onExport(exportPick)}
          >
            {picked === 0 ? "Бүгдийг экспортлох" : `Сонгосон ${picked}-г экспортлох`}
          </Button>
        )}
      </div>

      <section className="flex flex-col gap-3">
        <h3 className="font-display text-base font-semibold">
          Богино видео <span className="text-ink-3">({suggestions.shorts.length})</span>
        </h3>
        <div className="grid gap-3 lg:grid-cols-3">
          {suggestions.shorts.map((short) => {
            const seconds = totalCutSeconds(short.cuts);
            const inRange = seconds >= FIT_MIN && seconds <= FIT_MAX;
            const on = shorts.includes(short.id);
            return (
              <Card
                key={short.id}
                className={`flex flex-col gap-3 p-4 transition-colors ${on ? "border-accent" : ""}`}
              >
                <button
                  type="button"
                  onClick={() => toggleShort(short.id)}
                  className="flex items-start gap-2.5 text-left"
                >
                  <span className="mt-0.5">
                    <Tick on={on} />
                  </span>
                  <span className="font-display text-[15px] font-medium leading-snug">
                    {short.title}
                  </span>
                </button>

                <p className="text-sm text-ink-2">{short.hook_text}</p>

                <div className="flex items-center gap-2">
                  <Badge tone={inRange ? "fit" : "warn"}>{Math.round(seconds)} сек</Badge>
                  <span className="text-xs text-ink-3">{short.cuts.length} огтлол</span>
                </div>

                <ol className="flex flex-col gap-1">
                  {short.cuts.map((cut, index) => (
                    <li key={`${cut.start}-${index}`} className="flex items-baseline gap-2 text-xs">
                      <span className="w-16 shrink-0 text-ink-3">
                        {CUT_ROLE_LABELS[cut.role] ?? cut.role}
                      </span>
                      <span className="tabular font-mono text-ink-2">
                        {timecode(cut.start)}–{timecode(cut.end)}
                      </span>
                    </li>
                  ))}
                </ol>

                {short.hashtags.length > 0 && (
                  <p className="text-xs text-ink-3">{short.hashtags.join(" ")}</p>
                )}

                {sourceUrl && (
                  <Button
                    className="mt-auto min-h-[44px] self-start px-3 text-[13px]"
                    onClick={() =>
                      setPreview({
                        title: short.title,
                        cuts: short.cuts.map((cut) => ({
                          start: cut.start,
                          end: cut.end,
                          label: CUT_ROLE_LABELS[cut.role] ?? cut.role,
                        })),
                      })
                    }
                  >
                    Урьдчилж үзэх
                  </Button>
                )}
              </Card>
            );
          })}
        </div>
      </section>

      {suggestions.youtube.length > 0 && (
        <section className="flex flex-col gap-3">
          <h3 className="font-display text-base font-semibold">
            YouTube хураангуй <span className="text-ink-3">({suggestions.youtube.length})</span>
          </h3>
          <div className="grid gap-3 lg:grid-cols-3">
            {suggestions.youtube.map((plan, index) => {
              const on = plans.includes(index);
              return (
                <Card
                  key={`${plan.title}-${index}`}
                  className={`flex flex-col gap-2 p-4 transition-colors ${on ? "border-accent" : ""}`}
                >
                  <button
                    type="button"
                    onClick={() => togglePlan(index)}
                    className="flex items-start gap-2.5 text-left"
                  >
                    <span className="mt-0.5">
                      <Tick on={on} />
                    </span>
                    <span className="font-display text-[15px] font-medium leading-snug">
                      {plan.title}
                    </span>
                  </button>
                  <p className="text-sm text-ink-2">{plan.throughline}</p>
                  <p className="tabular text-xs text-ink-3">
                    {plan.ranges.length} хэсэг · {Math.round(plan.total_duration / 60)} мин
                  </p>
                  {sourceUrl && (
                    <Button
                      className="mt-auto min-h-[44px] self-start px-3 text-[13px]"
                      onClick={() =>
                        setPreview({
                          title: plan.title,
                          cuts: plan.ranges.map((range, i) => ({
                            start: range.start,
                            end: range.end,
                            label: `Хэсэг ${i + 1}`,
                          })),
                        })
                      }
                    >
                      Урьдчилж үзэх
                    </Button>
                  )}
                </Card>
              );
            })}
          </div>
        </section>
      )}

      {preview && sourceUrl && (
        <CutPreview
          sourceUrl={sourceUrl}
          cuts={preview.cuts}
          title={preview.title}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}
