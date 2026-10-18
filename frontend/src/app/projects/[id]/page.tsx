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

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { useParams, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { errorMessage, useRequireAuth } from "@/lib/auth";
import { duration, LANGUAGE_LABELS, usd } from "@/lib/format";
import { useLeaveGuard } from "@/lib/unsaved";
import type { Output, Project } from "@/lib/types";
import { Alert, Badge, Button, Card, Empty, Skeleton } from "@/components/ui";
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
import { JobHistory } from "@/components/JobHistory";
import { SuggestCounts, type Counts } from "@/components/SuggestCounts";
import { JobProgress } from "@/components/JobProgress";
import { LanguagePanel } from "@/components/LanguagePanel";
import { OutputList } from "@/components/OutputList";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Shell } from "@/components/Shell";
import { SuggestionList } from "@/components/SuggestionList";
import { TranscriptEditor, type EditorHandle } from "@/components/TranscriptEditor";

/** What is on screen and not yet sent, per editor. Counted, not flagged: a
 *  confirmation that says "3 мөр" is one the producer can weigh. */
interface Unsaved {
  text: number;
  translation: number;
  export: boolean;
  style: boolean;
}
const CLEAN: Unsaved = { text: 0, translation: 0, export: false, style: false };

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
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmRedo, setConfirmRedo] = useState(false);
  const [confirmTranscribe, setConfirmTranscribe] = useState(false);
  const [confirmSuggest, setConfirmSuggest] = useState(false);
  const [unsaved, setUnsaved] = useState<Unsaved>(CLEAN);
  const [savingEdits, setSavingEdits] = useState(false);
  const textEditor = useRef<EditorHandle>(null);
  const translationEditor = useRef<EditorHandle>(null);
  // Every stage opened so far stays mounted, hidden, rather than unmounted on
  // the next click. Unmounting is what dropped a half-corrected transcript
  // the moment the producer glanced at the ideas: its edits lived in the
  // editor, and the editor was gone.
  const [opened, setOpened] = useState<Stage[]>([]);
  // The producer's choice, or null for "the default". Kept across the refresh
  // every settled job triggers — a count chosen and then silently reset to
  // three by the page reloading itself would be a choice that did not stick.
  const [chosen, setChosen] = useState<Counts | null>(null);

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

  // Stable, so an editor's report effect runs when its COUNT changes, not on
  // every render of this page.
  const onTextDirty = useCallback(
    (text: number) => setUnsaved((u) => (u.text === text ? u : { ...u, text })),
    [],
  );
  const onTranslationDirty = useCallback(
    (translation: number) =>
      setUnsaved((u) => (u.translation === translation ? u : { ...u, translation })),
    [],
  );
  const onExportDirty = useCallback(
    (dirty: boolean) => setUnsaved((u) => (u.export === dirty ? u : { ...u, export: dirty })),
    [],
  );
  const onStyleDirty = useCallback(
    (dirty: boolean) => setUnsaved((u) => (u.style === dirty ? u : { ...u, style: dirty })),
    [],
  );
  const guard = useLeaveGuard(
    unsaved.text > 0 || unsaved.translation > 0 || unsaved.export || unsaved.style,
  );

  // A translation tab left open on a video since declared Mongolian has no
  // cell to belong to; it falls back to the text it would have translated.
  const view: Stage = tab === "translation" && !project?.translation.needed ? "transcript" : tab;

  useEffect(() => {
    setOpened((prev) => (prev.includes(view) ? prev : [...prev, view]));
    // A hidden panel keeps its player, and a player keeps playing: the sound
    // of a video nobody can see, with no control on screen to stop it.
    document
      .querySelectorAll<HTMLMediaElement>(
        "[data-stage-panel][hidden] video, [data-stage-panel][hidden] audio",
      )
      .forEach((media) => media.pause());
  }, [view]);

  // The furthest step that actually has content, so returning to a project
  // lands where the work is rather than at the beginning, and a job that
  // gets further moves the page along with it.
  //
  // The ideas, not their translation, even once it is done: it follows them
  // on its own, and the next decision is which of them to export.
  const furthest: Stage | null = !project
    ? null
    : outputs.length
      ? "outputs"
      : project.suggestions?.shorts.length
        ? "suggestions"
        : project.transcript?.segments.length
          ? "transcript"
          : "source";
  // Only when that step CHANGES. Every save refreshes the project, and the
  // settings live on the first tab: moving on each refresh threw the producer
  // off the panel they had just saved, before they could see it was saved.
  const reached = useRef<Stage | null>(null);
  useEffect(() => {
    if (!furthest || furthest === reached.current) return;
    reached.current = furthest;
    setTab((current: Stage) => (current === "source" ? furthest : current));
  }, [furthest]);

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

  /** The rail's pinned save. The editor reports its own failure in place,
   *  beside the lines it could not save. */
  async function saveEdits(editor: RefObject<EditorHandle | null>) {
    setSavingEdits(true);
    try {
      await editor.current?.save();
    } finally {
      setSavingEdits(false);
    }
  }

  async function remove() {
    try {
      await api.deleteProject(projectId);
      router.push("/");
    } catch (err) {
      setConfirmDelete(false);
      setError(errorMessage(err));
    }
  }

  if (authLoading || !user || (!project && !error)) {
    // The whole page's shape, not one box of it. What arrives here is a
    // title, a rail and a panel, and a placeholder that only stands in for
    // the rail still lets the other two appear as a jump.
    return (
      <Shell>
        {/* One `role="status"` for the page — the rail's. Three regions all
            named "Ачаалж байна" would be announced three times for one wait. */}
        <div className="flex flex-col gap-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex flex-col gap-2">
              <Skeleton className="h-8 w-64" />
              <Skeleton className="h-4 w-40" />
            </div>
            <Skeleton className="h-11 w-32 rounded-md" />
          </div>
          <PipelineRailSkeleton />
          <div className="flex flex-col gap-4">
            <Skeleton className="aspect-video w-full max-w-3xl rounded-lg" />
            <Skeleton className="h-40 rounded-lg" />
          </div>
        </div>
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

  // The server's counts and the export guard's own verdict. Worked out here
  // instead, the page would disable a button the server accepts — or offer
  // one it refuses — the first time either rule changed.
  const translation = project.translation;

  // Said where the producer meets it: in the export's own cell and above the
  // per-idea export on the suggestions tab. The ways forward named are the
  // ones that work: subtitles can go out in the spoken language, a Mongolian
  // voice has nothing else to read.
  // Counted over the lines the ideas cut — the only ones an export shows.
  const translationBlocked = !translation.blocks_export
    ? null
    : translation.used_for.includes("voice")
      ? `Саналын ${translation.suggested_missing} мөр орчуулагдаагүй байна — монгол дуу орчуулгыг уншдаг. «Орчуулга» алхмыг дуусгана уу, эсвэл Экспортын тохиргооноос монгол дууг унтраана уу.`
      : `Саналын ${translation.suggested_missing} мөр орчуулагдаагүй тул монгол хадмалд цоорхой гарна. «Орчуулга» алхмыг дуусгана уу, эсвэл Эх видео → Экспортын тохиргооноос хадмалыг ярьсан хэлээр нь гаргана уу.`;
  const voiceOn = project.voice.on;
  const voiceBlocked = project.voice.blocked;

  // Every action that reads the SAVED project — the ideas, a translation,
  // an export — would act on what was saved, not on what is on screen. Held
  // until it is saved, with the reason where the button is.
  const unsavedParts = [
    unsaved.text ? `текстийн ${unsaved.text} мөр` : "",
    unsaved.translation ? `орчуулгын ${unsaved.translation} мөр` : "",
    unsaved.export ? "экспортын тохиргоо" : "",
    unsaved.style ? "хадмалын загвар" : "",
  ].filter(Boolean);
  const unsavedList = unsavedParts.join(", ");
  const exportUnsaved = unsavedList
    ? `${unsavedList[0].toUpperCase()}${unsavedList.slice(1)} хадгалагдаагүй байна — экспорт хадгалсан хувилбараар гарна. Эхлээд хадгална уу.`
    : null;
  const textUnsaved = unsaved.text ? `Текстийн ${unsaved.text} мөр хадгалагдаагүй байна` : null;
  const exportBlocked = exportUnsaved ?? translationBlocked ?? voiceBlocked;
  const pending = (n: number) => (n ? ` · ${n} хадгалаагүй` : "");

  // Every cell says what it HOLDS, in the producer's terms — and a stage that
  // cannot run yet says why in its own cell, rather than as a footnote under
  // a row of disabled buttons.
  const STAGES: StageDef[] = [
    {
      key: "source",
      label: "Эх видео",
      state: hasVideo ? "done" : "current",
      detail: hasVideo
        ? `${duration(project.video!.duration_sec)} · ${project.video!.width}×${project.video!.height}${
            unsaved.export || unsaved.style ? " · хадгалаагүй" : ""
          }`
        : "Боловсруулагдаж байна",
    },
    {
      key: "transcript",
      label: "Текст",
      state: hasTranscript ? "done" : hasVideo ? "current" : "blocked",
      detail: hasTranscript
        ? `${segments} мөр${pending(unsaved.text)}`
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
    // Only for a video not in Mongolian. After the ideas, not before them:
    // they are chosen from what was said, and the translation that follows
    // them on its own buys only the lines they cut — not the hour of video
    // around a minute of export.
    ...(translation.needed
      ? [
          {
            key: "translation",
            label: "Орчуулга",
            state: !hasTranscript
              ? "blocked"
              : translation.lines > 0 && translation.missing === 0
                ? "done"
                : !hasSuggestions
                  ? "blocked"
                  : translation.suggested_missing
                    ? "current"
                    : "done",
            detail: !hasTranscript
              ? "Текст бэлдсэний дараа"
              : translation.lines > 0 && translation.missing === 0
                ? `${translation.lines} мөр монголоор${pending(unsaved.translation)}`
                : !hasSuggestions
                  ? "Саналын дараа өөрөө"
                  : translation.suggested_missing
                    ? `${translation.suggested_lines - translation.suggested_missing}/${
                        translation.suggested_lines
                      } мөр${translation.blocks_export ? "" : " · заавал биш"}`
                    : `Саналын ${translation.suggested_lines} мөр монголоор`,
          } satisfies StageDef,
        ]
      : []),
    {
      key: "outputs",
      label: "Бэлэн видео",
      state: outputs.length
        ? "done"
        : hasSuggestions && !exportBlocked
          ? "current"
          : "blocked",
      detail: outputs.length
        ? `${outputs.length} файл`
        : !hasSuggestions
          ? "Саналын дараа"
          : exportUnsaved
            ? "Хадгалаагүй өөрчлөлттэй"
            : translationBlocked
              ? "Орчуулга дуустал"
              : voiceBlocked
                ? "Монгол дуу бэлэн биш"
                : "Саналаас экспортлоно",
    },
  ];

  // Held to the server's range on every render, not only when chosen: the
  // limits come with each read of the project, and a count chosen against an
  // older read must not be sent past a newer one.
  const limits = project.suggest_limits;
  const counts: Counts = {
    shorts: Math.min(chosen?.shorts ?? limits.shorts_default, limits.shorts_max),
    youtube: Math.min(chosen?.youtube ?? limits.youtube_default, limits.youtube_max),
  };

  // Read where `project` is still narrowed — a nested function declaration
  // loses that, and `project!` inside one is an assertion nobody rechecks.
  const suggestCost =
    project.spend.suggest_estimate_usd !== null
      ? {
          usd: project.spend.suggest_estimate_usd,
          samples: project.spend.suggest_samples,
          basisShorts: project.spend.suggest_basis_shorts,
          askingShorts: counts.shorts,
        }
      : undefined;

  // ONE action at a time, and it belongs to the stage that is OPEN — which
  // is what restores re-running a finished step. Three buttons where two are
  // disabled is a menu of things you cannot do; one button for the stage you
  // just clicked, disabled with its reason when it cannot run, is an answer.
  const running = busy || !!activeJob;

  /** Unsaved lines make SAVING the next move — and on a phone the editor's
   *  own button has scrolled away with the list above three hundred lines. */
  function saveAction(
    editor: RefObject<EditorHandle | null>,
    label: string,
    secondary?: RailAction["secondary"],
  ): RailAction {
    return {
      label,
      secondary,
      note: "Хадгалаагүй засвар санал, орчуулга, экспортод орохгүй. Хуудаснаас гарахад анхааруулна.",
      onRun: () => void saveEdits(editor),
      disabled: savingEdits,
      loading: savingEdits,
    };
  }

  function actionFor(stage: Stage): RailAction | undefined {
    switch (stage) {
      case "transcript": {
        // A finished stage moves the producer ON. Starting it over replaces
        // every line — hand corrections and translations with them — and is
        // billed again, so it is the secondary button and it asks first.
        const again = {
          label: "Яриаг дахин таних…",
          onRun: () => setConfirmTranscribe(true),
          disabled: running,
        };
        if (hasTranscript && unsaved.text) {
          return saveAction(textEditor, `Засварыг хадгалах (${unsaved.text} мөр)`, again);
        }
        if (hasTranscript) {
          return {
            label: "Санал руу шилжих",
            note: "Текст бэлэн — засаад хадгалж, санал руу шилжинэ. «Дахин таних» бүх мөрийг, гар засвар, орчуулгатай нь солино.",
            onRun: () => setTab("suggestions"),
            secondary: again,
          };
        }
        return {
          label: "Яриаг текст болгох",
          note: hasVideo
            ? "Илтгэгч тус бүрээр, үгийн нарийвчлалтай хугацаатай. Оролдлого тутам төлбөртэй."
            : "Видео бэлдэж дуустал хүлээнэ үү.",
          onRun: () => void run(() => api.transcribe(projectId)),
          disabled: !hasVideo || running,
          loading: busy,
        };
      }
      case "suggestions": {
        if (hasSuggestions) {
          const next: Stage = translation.blocks_export ? "translation" : "outputs";
          return {
            label: next === "translation" ? "Орчуулга руу шилжих" : "Бэлэн видео руу шилжих",
            note: textUnsaved
              ? `${textUnsaved} — шинэ санал хадгалсан текстээс гарна. Эхлээд хадгална уу.`
              : "Санал бэлэн — доороос сонгож экспортлоно. «Санал дахин авах» одоогийн саналыг бүхэлд нь солино.",
            onRun: () => setTab(next),
            secondary: {
              label: "Санал дахин авах…",
              onRun: () => setConfirmSuggest(true),
              disabled: running || !!textUnsaved,
            },
          };
        }
        return {
          label: "Санал боловсруулах",
          note: textUnsaved
            ? `${textUnsaved} — санал хадгалсан текстээс гарна. Эхлээд хадгална уу.`
            : hasTranscript
              ? `${segments} мөр текстээс ${counts.shorts} богино видео${
                  counts.youtube ? ` ба ${counts.youtube} YouTube хураангуй` : ""
                } санал болгоно. Материал хүрэлцэхгүй бол цөөнийг гаргана — сул санал нэмж тоог гүйцээхгүй. Оролдлого тутам төлбөртэй.`
              : "Эхлээд яриаг текст болгоно.",
          control: hasTranscript ? (
            <SuggestCounts
              limits={limits}
              value={counts}
              onChange={setChosen}
              disabled={running}
            />
          ) : undefined,
          // The only step whose price this system actually measures. The
          // recogniser bills per minute of audio and nothing here counts it,
          // so "Яриаг текст болгох" deliberately carries no figure rather
          // than a confident wrong one.
          cost: suggestCost,
          onRun: () => void run(() => api.suggest(projectId, counts)),
          disabled: !hasTranscript || running || !!textUnsaved,
          loading: busy,
        };
      }
      case "translation": {
        if (hasTranscript && unsaved.translation) {
          return saveAction(
            translationEditor,
            `Орчуулгыг хадгалах (${unsaved.translation} мөр)`,
          );
        }
        // The lines the ideas cut come first: they are all an export needs,
        // and what runs on its own after the suggestions — this is the same
        // run, for when that one left some behind (or an edit cleared them).
        const cutLines = hasSuggestions && translation.suggested_missing > 0;
        // With nothing missing the only move left is starting OVER, which
        // replaces hand-corrected lines too — so that one asks first.
        const redo = hasTranscript && translation.missing === 0;
        const partial = translation.missing > 0 && translation.missing < translation.lines;
        return {
          label: cutLines
            ? `Саналын ${translation.suggested_missing} мөрийг орчуулах`
            : redo
              ? "Дахин орчуулах"
              : partial
                ? `Үлдсэн ${translation.missing} мөрийг орчуулах`
                : "Бүтэн текстийг орчуулах",
          note: !hasTranscript
            ? "Эхлээд яриаг текст болгоно."
            : textUnsaved
              ? `${textUnsaved} — орчуулга хадгалсан текстээс хийгдэнэ. Эхлээд хадгална уу.`
              : cutLines
                ? "Зөвхөн саналд орсон хэсгүүдийн орчуулагдаагүй мөрүүдийг — экспортод хэрэгтэй нь тэд. Оролдлого тутам төлбөртэй."
                : redo
                  ? "Бүх мөрийг шинээр орчуулна — гараар зассан орчуулга ч солигдоно. Оролдлого тутам төлбөртэй."
                  : partial && hasSuggestions
                    ? "Саналд ороогүй мөрүүдийг — экспортод хэрэггүй, бүтэн текст хэрэгтэй үед л. Орчуулагдсан нь, гараар зассан нь ч хэвээр үлдэнэ. Оролдлого тутам төлбөртэй."
                    : partial
                      ? "Зөвхөн орчуулагдаагүй мөрүүдийг. Орчуулагдсан нь, гараар зассан нь ч хэвээр үлдэнэ. Оролдлого тутам төлбөртэй."
                      : !hasSuggestions
                        ? "Санал гарсны дараа саналд орсон мөрүүд өөрөө орчуулагдана. Энэ товч бүтэн текстийг одоо орчуулна — урт видеонд зардал их. Оролдлого тутам төлбөртэй."
                        : "Үгчлэн биш, утгаар нь — мөр бүрийг уншиж амжих урттай. Оролдлого тутам төлбөртэй.",
          onRun: cutLines
            ? () => void run(() => api.translate(projectId, { scope: "suggested" }))
            : redo
              ? () => setConfirmRedo(true)
              : () => void run(() => api.translate(projectId)),
          disabled: !hasTranscript || !translation.lines || running || !!textUnsaved,
          loading: busy,
        };
      }
      case "outputs":
        return {
          label: "Бүгдийг экспортлох",
          note: !hasSuggestions
            ? "Санал боловсруулсны дараа экспортлоно."
            : (exportBlocked ??
              (voiceOn
                ? "Монгол дуутай: шинэ мөр бүр ElevenLabs-т тэмдэгтээр төлбөртэй, өмнө нь үүсгэсэн мөр дахин төлөгдөхгүй. Санал таб дээрээс тус тусад нь ч экспортлож болно."
                : "Санал таб дээрээс тус тусад нь ч экспортлож болно.")),
          onRun: () => void run(() => api.exportAll(projectId)),
          disabled: !hasSuggestions || !!exportBlocked || running,
          loading: busy,
        };
      case "source":
        // Nothing to re-run here — the import happens once, on upload. So the
        // opening screen offers the pipeline's next real move instead of
        // nothing at all.
        if (!hasVideo) return undefined;
        if (!hasTranscript) return actionFor("transcript");
        if (!hasSuggestions) return actionFor("suggestions");
        // Only when the export would need it: a producer who chose
        // subtitles in the spoken language is not steered into a paid run.
        if (translation.blocks_export) return actionFor("translation");
        return actionFor("outputs");
    }
  }

  const action = actionFor(view);

  // Mounted from the first visit on and hidden, never unmounted, so nothing
  // typed into one is lost by looking at another (see `opened`). Fixed slots
  // in a fixed order: React keeps a panel's state only while it stays put.
  const panel = (stage: Stage, content: ReactNode) =>
    stage === view || opened.includes(stage) ? (
      <div data-stage-panel hidden={stage !== view}>
        {content}
      </div>
    ) : null;

  return (
    <Shell>
      <div className="flex flex-col gap-6 pb-20 sm:pb-0">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="font-display text-2xl font-semibold tracking-tight">{project.name}</h1>
            <p className="tabular mt-1 text-sm text-ink-3">
              {hasVideo
                ? `${duration(project.video!.duration_sec)} · ${project.video!.width}×${project.video!.height}`
                : "Видео боловсруулагдаж байна…"}
              {translation.needed ? ` · ${LANGUAGE_LABELS[project.language]} яриа` : ""}
            </p>
          </div>
          <Button tone="danger" onClick={() => setConfirmDelete(true)}>
            Төсөл устгах
          </Button>
        </header>

        {/* Before the button, not after the failed job. */}
        <ProviderWarnings />

        {error && <Alert>{error}</Alert>}

        <PipelineRail
          stages={STAGES}
          active={view}
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

        {panel(
          "source",
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
                sourceLanguage={translation.needed ? project.language : null}
                speakers={project.speakers}
                hasTranscript={hasTranscript}
                onSaved={() => void refresh()}
                onDirtyChange={onExportDirty}
              />
            </Card>
            <Card className="p-5">
              <h3 className="font-display text-base font-semibold">Видеоны хэл</h3>
              <p className="mt-1 mb-4 text-sm text-ink-3">
                Яриаг аль хэлээр танихыг шийднэ. Монголоос бусад хэлтэй видеог монгол руу
                орчуулж, хадмалыг монголоор гаргана.
              </p>
              <LanguagePanel
                projectId={projectId}
                language={project.language}
                hasTranscript={hasTranscript}
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
                onDirtyChange={onStyleDirty}
              />
            </Card>
          </div>,
        )}

        {panel(
          "transcript",
          hasTranscript ? (
            <TranscriptEditor
              ref={textEditor}
              projectId={projectId}
              segments={project.transcript!.segments}
              timingsEstimated={project.transcript!.timings_estimated}
              sourceUrl={project.media.source_url}
              language={project.language}
              translation={translation}
              onSaved={() => void refresh()}
              onDirtyChange={onTextDirty}
            />
          ) : (
            <Empty
              title="Текст хараахан алга"
              hint="«Яриаг текст болгох» дарж эхлүүлнэ үү."
            />
          ),
        )}

        {translation.needed &&
          panel(
            "translation",
            hasTranscript ? (
              <TranscriptEditor
                ref={translationEditor}
                field="translation"
                projectId={projectId}
                segments={project.transcript!.segments}
                timingsEstimated={project.transcript!.timings_estimated}
                sourceUrl={project.media.source_url}
                language={project.language}
                translation={translation}
                focusIds={translation.suggested_ids}
                onSaved={() => void refresh()}
                onDirtyChange={onTranslationDirty}
              />
            ) : (
              <Empty
                title="Орчуулах текст хараахан алга"
                hint="Эхлээд «Яриаг текст болгох» дарна."
              />
            ),
          )}

        {panel(
          "suggestions",
          hasSuggestions ? (
            <SuggestionList
              suggestions={project.suggestions!}
              sourceUrl={project.media.source_url}
              busy={busy || !!activeJob}
              blocked={exportBlocked}
              onExport={(pick) => void run(() => api.exportAll(projectId, pick))}
            />
          ) : (
            <Empty
              title="Санал хараахан алга"
              hint="Текст бэлэн болсны дараа «Санал боловсруулах» дарна."
            />
          ),
        )}

        {panel(
          "outputs",
          <OutputList projectId={projectId} outputs={outputs} onChanged={() => void refresh()} />,
        )}

        {project.transcript?.timings_estimated && view === "source" && (
          <Badge tone="warn">Зарим хугацаа ойролцоо</Badge>
        )}

        {/* What ran, how long it took, what it cost — and the error text of
            anything that failed, which used to survive one page view. */}
        <JobHistory
          jobs={project.jobs}
          spend={project.spend}
          limit={project.job_history_limit}
        />

        {/* Counted, not implied: a project is hours of work and real money,
            and `window.confirm` could say neither. */}
        {/* Asked, because nothing records which lines were corrected by hand:
            a fresh run replaces them all, and there is no undo. */}
        {confirmRedo && (
          <ConfirmDialog
            title="Бүх мөрийг дахин орчуулах уу?"
            lose={[`${translation.translated} мөрийн одоогийн орчуулга — гараар зассан нь ч мөн`]}
            confirmLabel="Дахин орчуулах"
            onConfirm={() => {
              setConfirmRedo(false);
              void run(() => api.translate(projectId, { force: true }));
            }}
            onCancel={() => setConfirmRedo(false)}
          />
        )}

        {/* The counted list is the point: a transcript is often an hour of
            hand correction, and this used to be one unconfirmed tap. The
            ideas and the rendered videos are kept — only the text goes. */}
        {confirmTranscribe && (
          <ConfirmDialog
            title="Яриаг дахин таних уу?"
            lose={[
              `${segments} мөр текст — гараар зассан нь ч мөн`,
              translation.translated ? `${translation.translated} мөрийн монгол орчуулга` : "",
              unsaved.text ? `Хадгалаагүй ${unsaved.text} мөрийн засвар` : "",
              unsaved.translation ? `Хадгалаагүй ${unsaved.translation} мөрийн орчуулга` : "",
            ].filter(Boolean)}
            confirmLabel="Дахин таних"
            onConfirm={() => {
              setConfirmTranscribe(false);
              void run(() => api.transcribe(projectId, { force: true }));
            }}
            onCancel={() => setConfirmTranscribe(false)}
          >
            <p className="text-sm text-ink-2">
              Таних нь дахин төлбөртэй. Санал, бэлэн видео хэвээр үлдэнэ.
            </p>
          </ConfirmDialog>
        )}

        {/* The counts are chosen here, where the replacement is confirmed:
            on a finished stage they no longer sit in the rail. */}
        {confirmSuggest && (
          <ConfirmDialog
            title="Санал дахин авах уу?"
            lose={[`Одоогийн ${shorts} богино · ${plans} хураангуйн санал`]}
            confirmLabel="Санал дахин авах"
            onConfirm={() => {
              setConfirmSuggest(false);
              void run(() => api.suggest(projectId, counts, { force: true }));
            }}
            onCancel={() => setConfirmSuggest(false)}
          >
            <SuggestCounts limits={limits} value={counts} onChange={setChosen} />
            <p className="text-sm text-ink-2">
              {suggestCost
                ? `Дахин төлбөр гарна — ≈ ${usd(suggestCost.usd)}, өмнөх ${suggestCost.samples} гүйлтийн хэмжилтээр.`
                : "Дахин төлбөр гарна."}{" "}
              Бэлэн видео хэвээр үлдэнэ.
            </p>
          </ConfirmDialog>
        )}

        {guard.leaveTo && (
          <ConfirmDialog
            title="Хадгалаагүй өөрчлөлтийг орхих уу?"
            lose={[
              unsaved.text ? `Текстийн ${unsaved.text} мөрийн засвар` : "",
              unsaved.translation ? `Орчуулгын ${unsaved.translation} мөр` : "",
              unsaved.export ? "Экспортын тохиргооны өөрчлөлт" : "",
              unsaved.style ? "Хадмалын загварын өөрчлөлт" : "",
            ].filter(Boolean)}
            confirmLabel="Орхиод гарах"
            onConfirm={guard.leave}
            onCancel={guard.stay}
          />
        )}

        {confirmDelete && (
          <ConfirmDialog
            title={`«${project.name}» төслийг бүхэлд нь устгах уу?`}
            lose={[
              hasTranscript ? `${segments} мөр хадмал текст` : "",
              hasSuggestions ? `${shorts} богино · ${plans} хураангуйн санал` : "",
              outputs.length ? `${outputs.length} бэлэн видео` : "",
              "Эх видео",
            ].filter(Boolean)}
            confirmLabel="Устгах"
            onConfirm={() => void remove()}
            onCancel={() => setConfirmDelete(false)}
          />
        )}
      </div>
    </Shell>
  );
}
