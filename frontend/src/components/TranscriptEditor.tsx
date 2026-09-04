"use client";

/**
 * Correct transcript text before asking the model for suggestions.
 *
 * The video sits beside the lines, because correcting a transcript is a
 * listening job. It used to live on a different tab: the producer read a line
 * that looked wrong, switched tabs, hunted for the timecode, listened,
 * switched back, and by then had lost the line. Clicking a line now seeks the
 * player to it, and the line under the playhead is marked as it plays.
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

import { useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { timecode } from "@/lib/format";
import type { Segment } from "@/lib/types";
import { Alert, Badge, Button } from "@/components/ui";

export function TranscriptEditor({
  projectId,
  segments,
  timingsEstimated,
  sourceUrl,
  onSaved,
}: {
  projectId: string;
  segments: Segment[];
  timingsEstimated: boolean;
  /** Signed and short-lived; absent while the import is still running, in
   *  which case the editor is the plain list it always was. */
  sourceUrl?: string | null;
  onSaved: () => void;
}) {
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [playing, setPlaying] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  const changed = useMemo(
    () => segments.filter((s) => edits[s.id] !== undefined && edits[s.id] !== s.text),
    [segments, edits],
  );

  function playFrom(segment: Segment) {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = segment.start;
    void video.play().catch(() => undefined);
  }

  /** Which line the playhead is inside. Linear over a few hundred segments
   *  on a timeupdate tick is nothing; a binary search here would be a
   *  cleverness nobody can read for a saving nobody can measure. */
  function onTime() {
    const at = videoRef.current?.currentTime ?? 0;
    const current = segments.find((s) => at >= s.start && at < s.end);
    setPlaying((prev) => (prev === (current?.id ?? null) ? prev : current?.id ?? null));
  }

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

  const lines = (
    <div className="overflow-hidden rounded-lg border border-rule">
      <ul className="max-h-[34rem] divide-y divide-rule-soft overflow-y-auto">
        {segments.map((segment) => {
          const value = edits[segment.id] ?? segment.text;
          const isChanged = value !== segment.text;
          const isPlaying = playing === segment.id;
          return (
            <li
              key={segment.id}
              className={`flex gap-2 px-2 py-2 transition-colors ${
                isPlaying ? "bg-accent-soft" : "bg-surface"
              }`}
            >
              {/* The timecode IS the seek control — the obvious thing to
                  click, and it needs no second column of its own. */}
              <button
                type="button"
                onClick={() => playFrom(segment)}
                disabled={!sourceUrl}
                aria-label={`${timecode(segment.start)}-аас тоглуулах`}
                className={`tabular min-h-[44px] w-16 shrink-0 rounded px-1 pt-1.5 text-left font-mono text-[11px] transition-colors ${
                  isPlaying ? "text-accent" : "text-ink-3"
                } ${sourceUrl ? "hover:bg-surface-2 hover:text-ink-2" : "cursor-default"}`}
              >
                {timecode(segment.start)}
              </button>
              <textarea
                value={value}
                rows={1}
                onChange={(e) => setEdits((prev) => ({ ...prev, [segment.id]: e.target.value }))}
                className={`min-h-[2.75rem] w-full resize-y rounded border bg-transparent px-2 py-1.5 text-sm text-ink ${
                  isChanged ? "border-warn/60" : "border-transparent hover:border-rule"
                }`}
              />
            </li>
          );
        })}
      </ul>
    </div>
  );

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
            className="min-h-[44px] px-4"
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

      {sourceUrl ? (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
          <div className="flex flex-col gap-2 lg:sticky lg:top-4 lg:self-start">
            <video
              ref={videoRef}
              src={sourceUrl}
              controls
              preload="metadata"
              playsInline
              onTimeUpdate={onTime}
              className="w-full rounded-lg bg-black"
            />
            <p className="text-xs text-ink-3">
              Цагийн тэмдэг дээр дарахад тэр мөрөөс тоглуулна. Тоглож буй мөр тодрон харагдана.
            </p>
          </div>
          {lines}
        </div>
      ) : (
        lines
      )}
    </div>
  );
}
