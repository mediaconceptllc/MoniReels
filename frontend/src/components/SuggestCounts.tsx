"use client";

/**
 * How many ideas to ask for.
 *
 * The count was fixed at three — in the model's schema, where no prompt could
 * change it. The range offered here is the server's (`suggest_limits`), never
 * worked out on this side: the shorts limit is arithmetic on the minimum
 * short length, the YouTube one a 20-minute gate, and a second copy of either
 * is how a number reaches the page that the server then refuses.
 */

import type { SuggestLimits } from "@/lib/types";
import { Field, Select } from "@/components/ui";

export interface Counts {
  shorts: number;
  youtube: number;
}

function range(from: number, to: number): number[] {
  return Array.from({ length: Math.max(0, to - from + 1) }, (_, i) => from + i);
}

/** Nothing to choose on a video that can hold only one short and no plans —
 *  a picker with a single option is a control that does nothing. */
export function hasChoice(limits: SuggestLimits): boolean {
  return limits.shorts_max > 1 || limits.youtube_max > 0;
}

export function SuggestCounts({
  limits,
  value,
  onChange,
  disabled,
}: {
  limits: SuggestLimits;
  value: Counts;
  onChange: (next: Counts) => void;
  disabled?: boolean;
}) {
  if (!hasChoice(limits)) return null;
  return (
    <div className="flex flex-wrap gap-3">
      <div className="w-36">
        <Field label="Богино видео">
          <Select
            value={value.shorts}
            disabled={disabled}
            onChange={(e) => onChange({ ...value, shorts: Number(e.target.value) })}
          >
            {range(1, limits.shorts_max).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {limits.youtube_max > 0 && (
        <div className="w-36">
          <Field label="YouTube хураангуй">
            <Select
              value={value.youtube}
              disabled={disabled}
              onChange={(e) => onChange({ ...value, youtube: Number(e.target.value) })}
            >
              {range(0, limits.youtube_max).map((n) => (
                <option key={n} value={n}>
                  {n === 0 ? "Хэрэггүй" : n}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      )}
    </div>
  );
}
