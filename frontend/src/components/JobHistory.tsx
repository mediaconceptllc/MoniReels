"use client";

/**
 * What this project has run, how long it took, and what it cost.
 *
 * `JobProgress` shows one job and then vanishes with it, so the record of a
 * morning's work — which exports were made, which attempt failed and why, how
 * much the model charged — existed only in the database. The error text was
 * the worst loss: a failed job explained itself once, and a page reload took
 * the explanation away.
 *
 * Collapsed by default because it is a record, not a control. It is a native
 * disclosure rather than a state hook: the browser already knows how to open
 * and close one, and it works before any JavaScript arrives.
 */

import { duration, relativeTime, usd } from "@/lib/format";
import { JOB_LABELS } from "@/lib/jobs";
import type { Job, JobState, ProjectSpend } from "@/lib/types";
import { Badge, TAP } from "@/components/ui";

const STATE_LABEL: Record<JobState, string> = {
  queued: "Дараалалд",
  running: "Явж байна",
  done: "Дууссан",
  failed: "Амжилтгүй",
  canceled: "Цуцлагдсан",
};

function stateTone(state: JobState): "fit" | "warn" | "danger" | "accent" | "default" {
  if (state === "done") return "fit";
  if (state === "failed") return "danger";
  if (state === "running") return "accent";
  if (state === "canceled") return "warn";
  return "default";
}

export function JobHistory({
  jobs,
  spend,
  limit,
}: {
  jobs: Job[];
  spend: ProjectSpend;
  limit: number;
}) {
  if (jobs.length === 0) return null;
  const truncated = jobs.length >= limit;

  return (
    <details className="overflow-hidden rounded-lg border border-rule bg-surface">
      <summary
        className={`${TAP} flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-2 text-sm font-medium text-ink hover:bg-surface-2`}
      >
        <span>
          Ажлын түүх <span className="tabular font-normal text-ink-3">({jobs.length})</span>
        </span>
        {/* The headline number, readable without opening the panel — it is
            the one thing somebody comes here for at a glance. */}
        <span className="tabular text-[13px] font-normal text-ink-2">
          {spend.priced_jobs > 0 ? `Хэмжигдсэн зарлага ${usd(spend.spent_usd)}` : "Зарлага хэмжигдээгүй"}
        </span>
      </summary>

      <ul className="divide-y divide-rule-soft border-t border-rule">
        {jobs.map((job) => {
          const cost = job.result?.llm?.cost_usd;
          const ran = job.result?.elapsed_sec;
          const voice = job.result?.voice;
          // Absent on an export made before the speech could be removed.
          const separated = voice?.beds_separated ?? 0;
          const bedsCached = voice?.beds_cached ?? 0;
          return (
            <li key={job.job_id} className="flex flex-col gap-1 px-4 py-2.5">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Badge tone={stateTone(job.state)}>{STATE_LABEL[job.state]}</Badge>
                <span className="text-sm text-ink">{JOB_LABELS[job.kind] ?? job.kind}</span>
                <span className="tabular text-xs text-ink-3">
                  {typeof ran === "number" ? duration(ran) : "—"}
                </span>
                {/* An absent cost is "nothing measured it", which is not the
                    same as free — so it is a dash, never $0.00. */}
                <span className="tabular text-xs text-ink-2">
                  {typeof cost === "number"
                    ? usd(cost)
                    : voice
                      ? `${voice.characters} тэмдэгт`
                      : "—"}
                </span>
                <span className="tabular ml-auto text-xs text-ink-3">
                  {relativeTime(job.finished_at ?? job.created_at)}
                </span>
              </div>
              {/* The reason a job failed used to survive exactly one page
                  view. It is the most useful line in the whole panel. */}
              {job.error && (
                <p className="font-mono text-[11px] leading-snug text-tally">{job.error}</p>
              )}
              {/* The lines a voice-over had to hurry or cut are the ones to
                  listen to before publishing — counted, so nobody has to
                  find them by ear. */}
              {voice && (
                <p className="tabular text-xs text-ink-3">
                  Монгол дуу: {voice.lines} мөр · шинээр {voice.synthesized} · хадгалснаас{" "}
                  {voice.cached}
                  {voice.sped_up > 0 && ` · ${voice.sped_up} хурдасгасан`}
                  {voice.cut > 0 && ` · ${voice.cut} таслагдсан`}
                  {voice.missing > 0 && ` · ${voice.missing} орчуулгагүй`}
                </p>
              )}
              {/* The separation is the one part of a dub that costs time
                  rather than money — the reason an export ran long. */}
              {separated + bedsCached > 0 && (
                <p className="tabular text-xs text-ink-3">
                  Эх яриа хассан: шинээр {separated} хэсэг
                  {separated > 0 && ` (${duration(voice?.bed_seconds ?? 0)})`} · хадгалснаас{" "}
                  {bedsCached}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {/* What these numbers do NOT cover, beside the numbers themselves.
          Each line is a real hole, not a disclaimer. */}
      <div className="flex flex-col gap-0.5 border-t border-rule bg-surface-2 px-4 py-2.5 text-xs text-ink-3">
        {jobs.some((job) => job.result?.voice) && (
          <p>
            Монгол дуу тэмдэгтээр тооцогддог. Долларын үнэ нь ElevenLabs-ийн багцаас хамаарах тул
            энд тэмдэгтийн тоогоор харуулна.
          </p>
        )}
        {!spend.stt_measured && (
          <p>
            Зөвхөн загварын зарлага хэмжигддэг. Яриа таних төлбөрийг систем тоолдоггүй тул тэр
            мөрүүд «—» гэж харагдана.
          </p>
        )}
        <p>{spend.keep_days} хоногоос хуучин ажил цэвэрлэгддэг тул нийт дүн түүгээр дутуу байж болно.</p>
        {truncated && <p>Сүүлийн {limit} ажлыг харуулав.</p>}
      </div>
    </details>
  );
}
