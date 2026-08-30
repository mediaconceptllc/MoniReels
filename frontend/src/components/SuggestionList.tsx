"use client";

/**
 * What the model proposed.
 *
 * A reel is 3-5 SEPARATE, non-contiguous cuts assembled in the order shown —
 * not one continuous excerpt — so each cut is listed with its own role and
 * span, and the total is summed. The total is the number that decides whether
 * a reel is usable (35-60s), so it is shown rather than left to be counted.
 */

import { CUT_ROLE_LABELS, timecode, totalCutSeconds } from "@/lib/format";
import type { Suggestions } from "@/lib/types";
import { Badge, Card } from "@/components/ui";

export function SuggestionList({ suggestions }: { suggestions: Suggestions }) {
  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-3">
        <h3 className="font-display text-base font-semibold">
          Богино видео <span className="text-ink-3">({suggestions.shorts.length})</span>
        </h3>
        <div className="grid gap-3 lg:grid-cols-3">
          {suggestions.shorts.map((short) => {
            const total = totalCutSeconds(short.cuts);
            const inRange = total >= 35 && total <= 60;
            return (
              <Card key={short.id} className="flex flex-col gap-3 p-4">
                <div>
                  <p className="font-display text-[15px] font-medium leading-snug">
                    {short.title}
                  </p>
                  <p className="mt-1.5 text-sm text-ink-2">{short.hook_text}</p>
                </div>

                <div className="flex items-center gap-2">
                  <Badge tone={inRange ? "fit" : "warn"}>
                    {Math.round(total)} сек
                  </Badge>
                  <span className="text-xs text-ink-3">{short.cuts.length} огтлол</span>
                </div>

                <ol className="flex flex-col gap-1">
                  {short.cuts.map((cut, index) => (
                    <li
                      key={`${cut.start}-${index}`}
                      className="flex items-baseline gap-2 text-xs"
                    >
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
            {suggestions.youtube.map((plan, index) => (
              <Card key={`${plan.title}-${index}`} className="flex flex-col gap-2 p-4">
                <p className="font-display text-[15px] font-medium leading-snug">{plan.title}</p>
                <p className="text-sm text-ink-2">{plan.throughline}</p>
                <p className="tabular text-xs text-ink-3">
                  {plan.ranges.length} хэсэг · {Math.round(plan.total_duration / 60)} мин
                </p>
              </Card>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
