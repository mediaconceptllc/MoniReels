"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { errorMessage, useRequireAuth } from "@/lib/auth";
import { duration, relativeTime } from "@/lib/format";
import type { ProjectSummary, QueueStatus } from "@/lib/types";
import { Alert, Badge, Card, Empty, Spinner } from "@/components/ui";
import { NewProject } from "@/components/NewProject";
import { Shell } from "@/components/Shell";

export default function ProjectsPage() {
  const { user, loading: authLoading } = useRequireAuth();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [queue, setQueue] = useState<QueueStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [list, status] = await Promise.all([
        api.listProjects(),
        api.queueStatus().catch(() => null),
      ]);
      setProjects(list);
      setQueue(status);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    if (user) void refresh();
  }, [user, refresh]);

  if (authLoading || !user) {
    return (
      <Shell>
        <Spinner />
      </Shell>
    );
  }

  return (
    <Shell>
      <div className="flex flex-col gap-6">
        {/* Waiting work with nothing alive to do it means the worker service
            is down. Without this, the only symptom is a job that sits at 0%
            forever with no explanation anywhere in the interface. */}
        {queue?.stalled && (
          <Alert tone="warn">
            Ажлын дараалалд {queue.waiting} ажил хүлээж байгаа ч ажиллаж буй worker алга.
            Worker сервисээ шалгана уу.
          </Alert>
        )}

        <NewProject onCreated={() => void refresh()} />

        <section className="flex flex-col gap-3">
          <h2 className="font-display text-base font-semibold">Төслүүд</h2>

          {error && <Alert>{error}</Alert>}

          {!projects && !error && <Spinner />}

          {projects?.length === 0 && (
            <Empty
              title="Одоогоор төсөл алга"
              hint="Дээрээс видео сонгож эхний төслөө үүсгэнэ үү."
            />
          )}

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {projects?.map((project) => (
              <Link key={project.id} href={`/projects/${project.id}`} className="block">
                <Card className="flex h-full flex-col overflow-hidden transition-colors hover:border-accent">
                  {/* Import has always made a thumbnail; the list never asked
                      for one, so a wall of VIDEO projects read as a wall of
                      text. The frame is drawn either way so the grid does not
                      go ragged while an import is still running. */}
                  <div className="relative aspect-video bg-ink/90">
                    {project.thumbnail_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={project.thumbnail_url}
                        alt=""
                        loading="lazy"
                        className="h-full w-full object-cover"
                      />
                    ) : (
                      <span className="flex h-full items-center justify-center text-xs text-paper/60">
                        {project.has_video ? "Хальс бэлдэж байна" : "Видеогүй"}
                      </span>
                    )}
                    {project.duration_sec > 0 && (
                      <span className="tabular absolute bottom-1.5 right-1.5 rounded bg-ink/75 px-1.5 py-0.5 font-mono text-[11px] text-paper">
                        {duration(project.duration_sec)}
                      </span>
                    )}
                  </div>
                  <div className="flex flex-1 flex-col gap-2 p-3.5">
                    <p className="font-display text-[15px] font-medium leading-snug text-ink">
                      {project.name}
                    </p>
                    {/* One state, not four badges of equal weight competing to
                        be read. The furthest stage reached is the answer to
                        "where is this project", which is the only question a
                        list card is asked. */}
                    <div className="mt-auto flex items-center gap-2">
                      <Badge tone={stateTone(project)}>{stateLabel(project)}</Badge>
                      <span className="tabular text-xs text-ink-3">
                        {relativeTime(project.updated_at)}
                      </span>
                    </div>
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        </section>
      </div>
    </Shell>
  );
}

/** The furthest stage a project has reached, said once.
 *
 * Four badges of equal weight ("Видео", "Текст", "Санал", "3 бэлэн") made the
 * reader assemble the answer themselves, and the pipeline is strictly ordered
 * — so the last stage reached IS the state. */
function stateLabel(p: ProjectSummary): string {
  if (p.n_outputs > 0) return `${p.n_outputs} видео бэлэн`;
  if (p.has_suggestions) return "Санал бэлэн";
  if (p.has_transcript) return "Текст бэлэн";
  if (p.has_video) return "Видео орсон";
  return "Видеогүй";
}

function stateTone(p: ProjectSummary): "fit" | "accent" | "default" | "warn" {
  if (p.n_outputs > 0) return "fit";
  if (p.has_suggestions) return "accent";
  if (p.has_transcript) return "accent";
  if (p.has_video) return "default";
  return "warn";
}
