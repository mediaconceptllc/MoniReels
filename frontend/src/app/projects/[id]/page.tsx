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
import { Alert, Badge, Button, Card, Empty } from "@/components/ui";
import {
  PipelineRail,
  PipelineRailSkeleton,
  type RailAction,
  type Stage,
  type StageDef,
} from "@/components/PipelineRail";
import { ProviderWarnings } from "@/components/ProviderWarnings";
import { ExportSettingsPanel } from "@/components/ExportSettingsPanel";
import { SubtitleStylePanel } from "@/components/SubtitleStylePanel";
import { JobProgress } from "@/components/JobProgress";
import { OutputList } from "@/components/OutputList";
import { Shell } from "@/components/Shell";
import { SuggestionList } from "@/components/SuggestionList";
import { TranscriptEditor } from "@/components/TranscriptEditor";

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
  const [tab, setTab] = useState<Stage>("source");

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
    setTab((current: Stage) => {
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
    // The rail's own placeholder, not a spinner alone on a blank page: this
    // page reloads itself every time a job settles, and the layout used to
    // vanish and come back on each one.
    return (
      <Shell>
        <PipelineRailSkeleton />
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

  const segments = project.transcript?.segments.length ?? 0;
  const shorts = project.suggestions?.shorts.length ?? 0;
  const plans = project.suggestions?.youtube.length ?? 0;

  // Every cell says what it HOLDS, in the producer's terms — and a stage that
  // cannot run yet says why in its own cell, rather than as a footnote under
  // a row of disabled buttons.
  const STAGES: StageDef[] = [
    {
      key: "source",
      label: "Эх видео",
      state: hasVideo ? "done" : "current",
      detail: hasVideo
        ? `${duration(project.video!.duration_sec)} · ${project.video!.width}×${project.video!.height}`
        : "Боловсруулагдаж байна",
    },
    {
      key: "transcript",
      label: "Текст",
      state: hasTranscript ? "done" : hasVideo ? "current" : "blocked",
      detail: hasTranscript
        ? `${segments} мөр`
        : hasVideo
          ? "Дараагийн алхам"
          : "Видео бэлдэж дуустал",
    },
    {
      key: "suggestions",
      label: "Санал",
      state: hasSuggestions ? "done" : hasTranscript ? "current" : "blocked",
      detail: hasSuggestions
        ? `${shorts} богино · ${plans} хураангуй`
        : hasTranscript
          ? "Дараагийн алхам"
          : "Текст бэлдсэний дараа",
    },
    {
      key: "outputs",
      label: "Бэлэн видео",
      state: outputs.length ? "done" : hasSuggestions ? "current" : "blocked",
      detail: outputs.length
        ? `${outputs.length} файл`
        : hasSuggestions
          ? "Саналаас экспортлоно"
          : "Саналын дараа",
    },
  ];

  // ONE action at a time, and it belongs to the stage that is OPEN — which
  // is what restores re-running a finished step. Three buttons where two are
  // disabled is a menu of things you cannot do; one button for the stage you
  // just clicked, disabled with its reason when it cannot run, is an answer.
  const running = busy || !!activeJob;

  function actionFor(stage: Stage): RailAction | undefined {
    switch (stage) {
      case "transcript":
        return {
          label: hasTranscript ? "Яриаг дахин таних" : "Яриаг текст болгох",
          note: hasVideo
            ? "Илтгэгч тус бүрээр, үгийн нарийвчлалтай хугацаатай. Оролдлого тутам төлбөртэй."
            : "Видео бэлдэж дуустал хүлээнэ үү.",
          onRun: () => void run(() => api.transcribe(projectId)),
          disabled: !hasVideo || running,
          loading: busy,
        };
      case "suggestions":
        return {
          label: hasSuggestions ? "Санал дахин авах" : "Санал боловсруулах",
          note: hasTranscript
            ? `${segments} мөр текстээс богино видео, YouTube хураангуйн санал гаргана. Оролдлого тутам төлбөртэй.`
            : "Эхлээд яриаг текст болгоно.",
          onRun: () => void run(() => api.suggest(projectId)),
          disabled: !hasTranscript || running,
          loading: busy,
        };
      case "outputs":
        return {
          label: "Бүгдийг экспортлох",
          note: hasSuggestions
            ? "Санал таб дээрээс тус тусад нь ч экспортлож болно."
            : "Санал боловсруулсны дараа экспортлоно.",
          onRun: () => void run(() => api.exportAll(projectId)),
          disabled: !hasSuggestions || running,
          loading: busy,
        };
      case "source":
        // Nothing to re-run here — the import happens once, on upload. So the
        // opening screen offers the pipeline's next real move instead of
        // nothing at all.
        if (!hasVideo) return undefined;
        if (!hasTranscript) return actionFor("transcript");
        if (!hasSuggestions) return actionFor("suggestions");
        return actionFor("outputs");
    }
  }

  const action = actionFor(tab);

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

        <PipelineRail
          stages={STAGES}
          active={tab}
          onSelect={setTab}
          action={action}
        >
          {activeJob ? (
            <JobProgress
              jobId={activeJob}
              onSettled={() => {
                setActiveJob(null);
                void refresh();
              }}
            />
          ) : undefined}
        </PipelineRail>

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
            <SuggestionList
              suggestions={project.suggestions!}
              sourceUrl={project.media.source_url}
              busy={busy || !!activeJob}
              onExport={(pick) => void run(() => api.exportAll(projectId, pick))}
            />
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
