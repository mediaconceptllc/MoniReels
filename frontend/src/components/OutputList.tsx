"use client";

/**
 * Finished renders.
 *
 * The player and the download button use DIFFERENT urls on purpose: the
 * download one carries a Content-Disposition that makes the browser save the
 * file instead of playing it, so a single link cannot serve both.
 */

import { useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { OUTPUT_KIND_LABELS, fileSize } from "@/lib/format";
import type { Output } from "@/lib/types";
import { Alert, Badge, Button, Card, Empty } from "@/components/ui";

export function OutputList({
  projectId,
  outputs,
  onChanged,
}: {
  projectId: string;
  outputs: Output[];
  onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  if (outputs.length === 0) {
    return (
      <Empty
        title="Бэлэн видео алга"
        hint="Санал боловсруулсны дараа «Бүгдийг экспортлох» дарж видеонуудаа гаргана."
      />
    );
  }

  async function remove(output: Output) {
    if (!window.confirm(`«${output.title || "Гарц"}» устгах уу? Буцаах боломжгүй.`)) return;
    setDeleting(output.id);
    setError(null);
    try {
      await api.deleteOutput(projectId, output.id);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {error && <Alert>{error}</Alert>}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {outputs.map((output) => (
          <Card key={output.id} className="flex flex-col gap-3 p-3">
            <video
              controls
              preload="metadata"
              src={output.play_url}
              className="w-full rounded bg-black"
            />
            <div className="flex flex-col gap-1">
              <p className="text-sm font-medium leading-snug text-ink">
                {output.title || "Гарц"}
              </p>
              <div className="flex items-center gap-2">
                <Badge tone="accent">{OUTPUT_KIND_LABELS[output.kind] ?? output.kind}</Badge>
                <span className="tabular text-xs text-ink-3">{fileSize(output.size_bytes)}</span>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <a href={output.download_url} className="inline-flex">
                <Button className="px-2.5 py-1.5 text-xs">Татах</Button>
              </a>
              {output.srt_url && (
                <a href={output.srt_url} className="inline-flex">
                  <Button tone="quiet" className="px-2.5 py-1.5 text-xs">
                    Хадмал
                  </Button>
                </a>
              )}
              <Button
                tone="danger"
                className="ml-auto px-2.5 py-1.5 text-xs"
                loading={deleting === output.id}
                onClick={() => void remove(output)}
              >
                Устгах
              </Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
