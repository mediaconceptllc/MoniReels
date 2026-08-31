"use client";

/**
 * One project, as a pipeline the user walks down.
 *
 * The order is fixed by real dependencies, not by taste: nothing can be
 * transcribed before a video is imported, nothing can be suggested before
 * there is text to read, and nothing can be exported before there are
 * suggestions. Each step therefore shows why it is not yet available rather
 * than presenting a button that returns a 400.
 */

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { errorMessage, useRequireAuth } from "@/lib/auth";
import { duration } from "@/lib/format";
import type { Output, Project } from "@/lib/types";
import { Alert, Badge, Button, Card, Empty, Spinner } from "@/components/ui";
import { ProviderWarnings } from "@/components/ProviderWarnings";
import { ExportSettingsPanel } from "@/components/ExportSettingsPanel";
import { SubtitleStylePanel } from "@/components/SubtitleStylePanel";
import { JobProgress } from "@/components/JobProgress";
import { OutputList } from "@/components/OutputList";
import { Shell } from "@/components/Shell";
import { SuggestionList } from "@/components/SuggestionList";
import { TranscriptEditor } from "@/components/TranscriptEditor";

type Tab = "source" | "transcript" | "suggestions" | "outputs";

export default function ProjectPage() {
  const { user, loading: authLoading } = useRequireAuth();
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const projectId = params.id;

  const [project, setProject] = useState<Project | null>(null);
  const [outputs, setOutputs] = useState<Output[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<Tab>("source");

  const refresh = useCallback(async () => {
    try {
      const [next, outs] = await Promise.all([
        api.getProject(projectId),
        api.listOutputs(projectId).catch(() => [] as Output[]),
      ]);
      setProject(next);
      setOutputs(outs);
      setError(null);

      // A job may still be running from an earlier visit — this page must
      // reattach to it rather than look idle while work continues.
      const live = next.jobs.find((job) => job.state === "queued" || job.state === "running");
      setActiveJob((current) => current ?? live?.job_id ?? null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [projectId]);

  useEffect(() => {
    if (user) void refresh();
  }, [user, refresh]);

  // Pick the furthest step that actually has content, so returning to a
  // project lands where the work is rather than at the beginning.
  useEffect(() => {
    if (!project) return;
    setTab((current) => {
      if (current !== "source") return current;
      if (outputs.length) return "outputs";
      if (project.suggestions?.shorts.length) return "suggestions";
      if (project.transcript?.segments.length) return "transcript";
      return "source";
    });
  }, [project, outputs.length]);

  async function run(action: () => Promise<{ job_id: string }>) {
    setBusy(true);
    setError(null);
    try {
      const { job_id } = await action();
      setActiveJob(job_id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!project) return;
    if (!window.confirm(`«${project.name}» төслийг бүхэлд нь устгах уу? Буцаах боломжгүй.`)) return;
    try {
      await api.deleteProject(projectId);
      router.push("/");
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  if (authLoading || !user || (!project && !error)) {
    return (
      <Shell>
        <Spinner />
      </Shell>
    );
  }

  if (!project) {
    return (
      <Shell>
        <Alert>{error}</Alert>
      </Shell>
    );
  }

  const hasVideo = !!project.video;
  const hasTranscript = !!project.transcript?.segments.length;
  const hasSuggestions = !!project.suggestions?.shorts.length;

  const TABS: { key: Tab; label: string; ready: boolean }[] = [
    { key: "source", label: "Эх видео", ready: true },
    { key: "transcript", label: "Текст", ready: hasTranscript },
    { key: "suggestions", label: "Санал", ready: hasSuggestions },
    { key: "outputs", label: `Бэлэн (${outputs.length})`, ready: outputs.length > 0 },
  ];

  return (
    <Shell>
      <div className="flex flex-col gap-6">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="font-display text-2xl font-semibold tracking-tight">{project.name}</h1>
            <p className="tabular mt-1 text-sm text-ink-3">
              {hasVideo
                ? `${duration(project.video!.duration_sec)} · ${project.video!.width}×${project.video!.height}`
                : "Видео боловсруулагдаж байна…"}
            </p>
          </div>
          <Button tone="danger" onClick={remove}>
            Төсөл устгах
          </Button>
        </header>

        {/* Before the button, not after the failed job. */}
        <ProviderWarnings />

        {error && <Alert>{error}</Alert>}

        {activeJob && (
          <Card className="p-4">
            <JobProgress
              jobId={activeJob}
              onSettled={() => {
                setActiveJob(null);
                void refresh();
              }}
            />
          </Card>
        )}

        <Card className="flex flex-wrap items-center gap-2 p-4">
          <Button
            tone="primary"
            disabled={!hasVideo || busy || !!activeJob}
            loading={busy}
            onClick={() => void run(() => api.transcribe(projectId))}
          >
            {hasTranscript ? "Дахин таних" : "Яриаг текст болгох"}
          </Button>
          <Button
            disabled={!hasTranscript || busy || !!activeJob}
            onClick={() => void run(() => api.suggest(projectId))}
          >
            {hasSuggestions ? "Дахин санал авах" : "Санал боловсруулах"}
          </Button>
          <Button
            disabled={!hasSuggestions || busy || !!activeJob}
            onClick={() => void run(() => api.exportAll(projectId))}
          >
            Бүгдийг экспортлох
          </Button>

          {/* The reason a step is unavailable, stated once — otherwise a
              disabled button is just a dead end. */}
          {!hasVideo && (
            <span className="text-xs text-ink-3">Видео бэлдэж дуустал хүлээнэ үү.</span>
          )}
          {hasVideo && !hasTranscript && (
            <span className="text-xs text-ink-3">Эхлээд яриаг текст болгоно.</span>
          )}
          {hasTranscript && !hasSuggestions && (
            <span className="text-xs text-ink-3">Санал боловсруулсны дараа экспортлоно.</span>
          )}
        </Card>

        <nav className="flex flex-wrap gap-1 border-b border-rule">
          {TABS.map((item) => (
            <button
              key={item.key}
              onClick={() => setTab(item.key)}
              className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                tab === item.key
                  ? "border-accent text-ink"
                  : "border-transparent text-ink-3 hover:text-ink-2"
              }`}
            >
              {item.label}
              {!item.ready && item.key !== "source" && (
                <span className="ml-1.5 text-[11px] text-ink-3">—</span>
              )}
            </button>
          ))}
        </nav>

        {tab === "source" && (
          <div className="flex flex-col gap-6">
            {project.media.source_url ? (
              <video
                controls
                preload="metadata"
                poster={project.media.thumbnail_url ?? undefined}
                src={project.media.source_url}
                className="w-full max-w-3xl rounded-lg bg-black"
              />
            ) : (
              <Empty title="Видео боловсруулагдаж байна" hint="Хуулалт дууссаны дараа энд харагдана." />
            )}
            <Card className="p-5">
              <h3 className="font-display text-base font-semibold">Экспортын тохиргоо</h3>
              <p className="mt-1 mb-4 text-sm text-ink-3">
                Экспортлохоос өмнө тохируулна. Дараагийн экспорт бүрд хэрэглэгдэнэ.
              </p>
              <ExportSettingsPanel
                projectId={projectId}
                settings={project.export}
                onSaved={() => void refresh()}
              />
            </Card>
            <Card className="p-5">
              <h3 className="font-display text-base font-semibold">Хадмалын загвар</h3>
              <p className="mt-1 mb-4 text-sm text-ink-3">
                Шатаасан хадмал болон .srt файлд хэрэглэгдэнэ.
              </p>
              <SubtitleStylePanel
                projectId={projectId}
                style={project.subtitle_style}
                onSaved={() => void refresh()}
              />
            </Card>
          </div>
        )}

        {tab === "transcript" &&
          (hasTranscript ? (
            <TranscriptEditor
              projectId={projectId}
              segments={project.transcript!.segments}
              timingsEstimated={project.transcript!.timings_estimated}
              onSaved={() => void refresh()}
            />
          ) : (
            <Empty
              title="Текст хараахан алга"
              hint="«Яриаг текст болгох» дарж эхлүүлнэ үү."
            />
          ))}

        {tab === "suggestions" &&
          (hasSuggestions ? (
            <SuggestionList suggestions={project.suggestions!} />
          ) : (
            <Empty
              title="Санал хараахан алга"
              hint="Текст бэлэн болсны дараа «Санал боловсруулах» дарна."
            />
          ))}

        {tab === "outputs" && (
          <OutputList projectId={projectId} outputs={outputs} onChanged={() => void refresh()} />
        )}

        {project.transcript?.timings_estimated && tab === "source" && (
          <Badge tone="warn">Зарим хугацаа ойролцоо</Badge>
        )}
      </div>
    </Shell>
  );
}
