"use client";

/**
 * Correct transcript text before asking the model for suggestions.
 *
 * Only the TEXT is editable. Timings come from our own cut boundaries and are
 * exact by construction, and the suggestion stage addresses segments by
 * index — so a shifted boundary would silently move every cut the model
 * proposes. The server rejects timing edits regardless; the interface simply
 * does not offer them.
 *
 * Only changed lines are sent. Posting the whole table would make one stale
 * tab overwrite corrections another made.
 */

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { timecode } from "@/lib/format";
import type { Segment } from "@/lib/types";
import { Alert, Badge, Button } from "@/components/ui";

export function TranscriptEditor({
  projectId,
  segments,
  timingsEstimated,
  onSaved,
}: {
  projectId: string;
  segments: Segment[];
  timingsEstimated: boolean;
  onSaved: () => void;
}) {
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const changed = useMemo(
    () => segments.filter((s) => edits[s.id] !== undefined && edits[s.id] !== s.text),
    [segments, edits],
  );

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.updateTranscript(
        projectId,
        changed.map((s) => ({ id: s.id, text: edits[s.id] })),
      );
      setEdits({});
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h3 className="font-display text-base font-semibold">Хадмал текст</h3>
          <span className="tabular text-xs text-ink-3">{segments.length} мөр</span>
        </div>
        <div className="flex items-center gap-2">
          {changed.length > 0 && <Badge tone="warn">{changed.length} мөр өөрчлөгдсөн</Badge>}
          {saved && changed.length === 0 && <Badge tone="fit">✓ Хадгалагдсан</Badge>}
          <Button
            tone="primary"
            onClick={save}
            loading={saving}
            disabled={changed.length === 0}
          >
            Хадгалах
          </Button>
        </div>
      </div>

      {/* An honest caveat rather than a silent one: when a chunk held several
          sentences, the split between them inside that chunk was estimated.
          The chunk's own start and end are still exact. */}
      {timingsEstimated && (
        <Alert tone="warn">
          Зарим мөрийн доторх өгүүлбэрийн хуваарь ойролцоо тооцоологдсон. Хэсгийн эхлэл,
          төгсгөл нь харин яг таарна.
        </Alert>
      )}

      {error && <Alert>{error}</Alert>}

      <div className="max-h-[28rem] overflow-y-auto rounded-lg border border-rule">
        <ul className="divide-y divide-rule-soft">
          {segments.map((segment) => {
            const value = edits[segment.id] ?? segment.text;
            const isChanged = value !== segment.text;
            return (
              <li key={segment.id} className="flex gap-3 bg-surface px-3 py-2">
                <span className="tabular w-14 shrink-0 pt-1.5 font-mono text-[11px] text-ink-3">
                  {timecode(segment.start)}
                </span>
                <textarea
                  value={value}
                  rows={1}
                  onChange={(e) =>
                    setEdits((prev) => ({ ...prev, [segment.id]: e.target.value }))
                  }
                  className={`min-h-[2.25rem] w-full resize-y rounded border bg-transparent px-2 py-1 text-sm text-ink ${
                    isChanged ? "border-warn/60" : "border-transparent hover:border-rule"
                  }`}
                />
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
