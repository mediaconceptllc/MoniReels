"use client";

/**
 * The queue, for the person who has to answer for it.
 *
 * Listing jobs and putting a failed one back existed only in Swagger, and the
 * queue's health could be judged from nothing at all: no success rate, no
 * idea how long a render takes or how long anything waits before it starts.
 * Every number here is the server's, measured from the rows themselves — this
 * page computes nothing it then presents as a fact.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { duration, fileSize, relativeTime } from "@/lib/format";
import { JOB_LABELS } from "@/lib/jobs";
import type { AdminJob, AdminJobsPage, JobState, JobStats, JobStatsRow, QueueStatus } from "@/lib/types";
import { Alert, Badge, Button, Card, Field, Loading, Select, Skeleton } from "@/components/ui";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { STATE_LABEL, stateTone } from "@/components/JobHistory";

const PAGE = 25;
const WINDOWS = [1, 7, 30];
const STATES: JobState[] = ["queued", "running", "done", "failed", "canceled"];

/** Seconds as a person reads them. Unlike `duration`, a measured 0 is shown
 *  as 0 — a job claimed the instant it was queued waited no time, and a dash
 *  there would read as "not measured". */
function secs(value: number | null): string {
  if (value === null) return "—";
  if (value < 10) return `${value.toFixed(1)} сек`;
  if (value < 60) return `${Math.round(value)} сек`;
  return duration(value);
}

function percent(value: number | null): string {
  return value === null ? "—" : `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md border border-rule-soft bg-surface-2 px-3 py-2.5">
      <span className="text-xs text-ink-3">{label}</span>
      <span className="tabular font-display text-lg font-semibold text-ink">{value}</span>
      {sub && <span className="tabular text-xs text-ink-3">{sub}</span>}
    </div>
  );
}

function StatsCard({ stats, days, onDays }: { stats: JobStats | null; days: number; onDays: (d: number) => void }) {
  const o: JobStatsRow | undefined = stats?.overall;
  return (
    <Card className="p-5">
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="font-display text-lg font-semibold text-ink">Ажлын үзүүлэлт</h2>
            <p className="mt-1 text-sm text-ink-3">Дууссан, амжилтгүй, цуцлагдсан ажлаас хэмжив.</p>
          </div>
          <Field label="Хугацаа">
            <Select value={days} onChange={(e) => onDays(Number(e.target.value))}>
              {WINDOWS.map((d) => (
                <option key={d} value={d}>
                  Сүүлийн {d} хоног
                </option>
              ))}
            </Select>
          </Field>
        </div>

        {!stats || !o ? (
          <Loading className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Skeleton className="h-20 rounded-md" />
            <Skeleton className="h-20 rounded-md" />
            <Skeleton className="h-20 rounded-md" />
            <Skeleton className="h-20 rounded-md" />
          </Loading>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Tile
                label="Амжилт"
                value={percent(o.success_pct)}
                sub={`${o.done} дууссан · ${o.failed} амжилтгүй`}
              />
              <Tile label="Ажилласан (дундаж)" value={secs(o.run_p50_sec)} sub={`95%: ${secs(o.run_p95_sec)}`} />
              <Tile
                label="Хүлээсэн (дундаж)"
                value={secs(o.wait_p50_sec)}
                sub={`95%: ${secs(o.wait_p95_sec)} · ${o.wait_samples} ажил`}
              />
              <Tile
                label="Одоо"
                value={`${stats.queue.waiting} хүлээж · ${stats.queue.running} явж`}
                sub={
                  stats.queue.oldest_waiting_sec !== null
                    ? `Хамгийн удаан хүлээж буй: ${secs(stats.queue.oldest_waiting_sec)}`
                    : `${stats.queue.live_workers} амьд ажил`
                }
              />
            </div>

            {stats.queue.stalled && (
              <Alert tone="warn">
                Ажил хүлээж байгаа ч амьд worker алга — worker үйлчилгээ зогссон байж магадгүй.
              </Alert>
            )}

            {stats.kinds.length === 0 ? (
              <p className="text-sm text-ink-3">Энэ хугацаанд дууссан ажил алга.</p>
            ) : (
              // Scrolls inside its own box: a phone is narrower than eight
              // columns, and a page that scrolls sideways loses its header.
              <div className="overflow-x-auto rounded-md border border-rule">
                <table className="w-full min-w-[40rem] text-left text-sm">
                  <thead className="bg-surface-2 text-xs text-ink-3">
                    <tr>
                      <th className="px-3 py-2 font-medium">Төрөл</th>
                      <th className="px-3 py-2 font-medium">Амжилт</th>
                      <th className="px-3 py-2 font-medium">Дууссан</th>
                      <th className="px-3 py-2 font-medium">Амжилтгүй</th>
                      <th className="px-3 py-2 font-medium">Цуцалсан</th>
                      <th className="px-3 py-2 font-medium">Ажилласан · 50% / 95%</th>
                      <th className="px-3 py-2 font-medium">Хүлээсэн · 50% / 95%</th>
                    </tr>
                  </thead>
                  <tbody className="tabular divide-y divide-rule-soft">
                    {stats.kinds.map((k) => (
                      <tr key={k.kind}>
                        <td className="px-3 py-2 text-ink">{JOB_LABELS[k.kind] ?? k.kind}</td>
                        <td className="px-3 py-2 text-ink">{percent(k.success_pct)}</td>
                        <td className="px-3 py-2 text-ink-2">{k.done}</td>
                        <td className="px-3 py-2 text-ink-2">{k.failed}</td>
                        <td className="px-3 py-2 text-ink-2">{k.canceled}</td>
                        <td className="px-3 py-2 text-ink-2">
                          {secs(k.run_p50_sec)} / {secs(k.run_p95_sec)}
                        </td>
                        <td className="px-3 py-2 text-ink-2">
                          {secs(k.wait_p50_sec)} / {secs(k.wait_p95_sec)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* What the numbers do not say, beside them. Each line is a real
                limit of the measurement, not a disclaimer. */}
            <div className="flex flex-col gap-0.5 text-xs text-ink-3">
              <p>Цуцалсан ажил амжилтын хувьд орохгүй — тэр нь хүний шийдвэр, алдаа биш.</p>
              <p>
                Хүлээсэн хугацааг эхний оролдлогоороо амжилттай болсон ажлаас л хэмжинэ: бусдынх нь өмнөх
                амжилтгүй оролдлогыг багтаадаг.
              </p>
              {stats.days < days && (
                <p>
                  Ажил {stats.keep_days} хоногийн дараа цэвэрлэгддэг тул сүүлийн {stats.days} хоногийг
                  харуулав.
                </p>
              )}
            </div>
          </>
        )}
      </div>
    </Card>
  );
}

function DiskCard({ queue }: { queue: QueueStatus | null }) {
  if (!queue) return null;
  const worker = queue.worker_disk;
  const api = queue.api_disk;
  const low = worker !== null && worker.free_bytes < worker.min_free_bytes;
  return (
    <Card className="p-5">
      <div className="flex flex-col gap-3">
        <h2 className="font-display text-lg font-semibold text-ink">Диск</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <span className="text-[13px] font-medium text-ink-2">Worker — экспорт энд бичигдэнэ</span>
            {worker ? (
              <>
                <span className="tabular text-sm text-ink">
                  {fileSize(worker.free_bytes)} сул / {fileSize(worker.total_bytes)}
                </span>
                <span className="text-xs text-ink-3">Хэмжсэн: {relativeTime(worker.measured_at)}</span>
                {low && <Badge tone="warn">Сул зай доод хэмжээнээс бага</Badge>}
              </>
            ) : (
              <span className="text-sm text-ink-3">Worker хараахан мэдээлээгүй байна.</span>
            )}
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[13px] font-medium text-ink-2">API контейнер</span>
            <span className="tabular text-sm text-ink">
              {fileSize(api.free_bytes)} сул / {fileSize(api.total_bytes)}
            </span>
            <span className="text-xs text-ink-3">Энэ нь хүсэлт хүлээн авдаг машин — экспорт энд бичигддэггүй.</span>
          </div>
        </div>
      </div>
    </Card>
  );
}

function JobRow({ job, onRetry, busy }: { job: AdminJob; onRetry: (job: AdminJob) => void; busy: boolean }) {
  const ran = job.result?.elapsed_sec;
  return (
    <li className="flex flex-col gap-1.5 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Badge tone={stateTone(job.state)}>{STATE_LABEL[job.state]}</Badge>
        <span className="text-sm font-medium text-ink">{JOB_LABELS[job.kind] ?? job.kind}</span>
        {job.no_retry && <Badge tone="warn">Төлбөртэй</Badge>}
        <span className="tabular ml-auto text-xs text-ink-3">{relativeTime(job.created_at)}</span>
      </div>
      <p className="text-xs text-ink-2 [overflow-wrap:anywhere]">
        {job.project_name ?? "Төсөлгүй"}
        {job.owner && <span className="text-ink-3"> · {job.owner}</span>}
      </p>
      <p className="tabular text-xs text-ink-3">
        {job.attempts} оролдлого
        {typeof ran === "number" && ` · ${secs(ran)} ажилласан`}
        {job.requeued_at !== null && ` · Админ дахин эхлүүлсэн: ${relativeTime(job.requeued_at)}`}
      </p>
      {job.error && (
        <p className="font-mono text-[11px] leading-snug text-tally [overflow-wrap:anywhere]">{job.error}</p>
      )}
      {job.retryable && (
        <div>
          <Button loading={busy} onClick={() => onRetry(job)}>
            Дахин эхлүүлэх
          </Button>
        </div>
      )}
    </li>
  );
}

export function AdminJobs() {
  const [days, setDays] = useState(7);
  const [stats, setStats] = useState<JobStats | null>(null);
  const [queue, setQueue] = useState<QueueStatus | null>(null);
  const [page, setPage] = useState<AdminJobsPage | null>(null);
  const [state, setState] = useState("");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [asking, setAsking] = useState<AdminJob | null>(null);

  const loadStats = useCallback(async () => {
    try {
      const [s, q] = await Promise.all([api.jobStats(days), api.queueStatus()]);
      setStats(s);
      setQueue(q);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [days]);

  const loadJobs = useCallback(async () => {
    try {
      setPage(await api.adminJobs({ state, kind, limit: PAGE, offset }));
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [state, kind, offset]);

  useEffect(() => {
    void loadStats();
  }, [loadStats]);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  async function retry(job: AdminJob) {
    setBusy(job.job_id);
    setError(null);
    setNotice(null);
    try {
      await api.retryJob(job.job_id);
      setAsking(null);
      await Promise.all([loadJobs(), loadStats()]);
      setNotice(`«${JOB_LABELS[job.kind] ?? job.kind}» дахин дараалалд орлоо.`);
    } catch (err) {
      setError(errorMessage(err));
      setAsking(null);
    } finally {
      setBusy(null);
    }
  }

  const shownTo = page ? Math.min(page.offset + page.jobs.length, page.total) : 0;

  return (
    <div className="flex flex-col gap-6">
      {error && <Alert>{error}</Alert>}
      {notice && <Alert tone="accent">{notice}</Alert>}

      <StatsCard stats={stats} days={days} onDays={setDays} />
      <DiskCard queue={queue} />

      <Card className="p-5">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <h2 className="font-display text-lg font-semibold text-ink">Ажлууд</h2>
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Төлөв">
                <Select
                  value={state}
                  onChange={(e) => {
                    setState(e.target.value);
                    setOffset(0);
                  }}
                >
                  <option value="">Бүгд</option>
                  {STATES.map((s) => (
                    <option key={s} value={s}>
                      {STATE_LABEL[s]}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Төрөл">
                <Select
                  value={kind}
                  onChange={(e) => {
                    setKind(e.target.value);
                    setOffset(0);
                  }}
                >
                  <option value="">Бүгд</option>
                  {Object.entries(JOB_LABELS).map(([k, label]) => (
                    <option key={k} value={k}>
                      {label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button onClick={() => void Promise.all([loadJobs(), loadStats()])}>Шинэчлэх</Button>
            </div>
          </div>

          {!page ? (
            <Loading className="flex flex-col gap-3">
              <Skeleton className="h-20 rounded-md" />
              <Skeleton className="h-20 rounded-md" />
              <Skeleton className="h-20 rounded-md" />
            </Loading>
          ) : page.jobs.length === 0 ? (
            <p className="text-sm text-ink-3">Энэ шүүлтэд тохирох ажил алга.</p>
          ) : (
            <>
              <ul className="flex flex-col divide-y divide-rule-soft">
                {page.jobs.map((job) => (
                  <JobRow
                    key={job.job_id}
                    job={job}
                    busy={busy === job.job_id}
                    // A metered kind is a second bill; anything else simply
                    // runs again, as the button says.
                    onRetry={(j) => (j.no_retry ? setAsking(j) : void retry(j))}
                  />
                ))}
              </ul>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <span className="tabular text-xs text-ink-3">
                  {page.offset + 1}–{shownTo} / нийт {page.total}
                </span>
                <div className="flex gap-2">
                  <Button disabled={page.offset === 0} onClick={() => setOffset(Math.max(0, page.offset - PAGE))}>
                    Өмнөх
                  </Button>
                  <Button disabled={shownTo >= page.total} onClick={() => setOffset(page.offset + PAGE)}>
                    Дараах
                  </Button>
                </div>
              </div>
            </>
          )}
        </div>
      </Card>

      {asking && (
        <ConfirmDialog
          title={`«${JOB_LABELS[asking.kind] ?? asking.kind}»-г дахин эхлүүлэх үү?`}
          confirmLabel="Дахин эхлүүлэх"
          busy={busy === asking.job_id}
          onConfirm={() => void retry(asking)}
          onCancel={() => setAsking(null)}
        >
          <p className="text-sm text-ink-2">
            Энэ төрлийн ажил оролдлого бүрт төлбөр гардаг тул автоматаар дахин оролддоггүй. Дахин эхлүүлбэл
            шинээр төлбөр гарна — өмнөх оролдлого үр дүнгээ гаргасан байж ч болно.
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
