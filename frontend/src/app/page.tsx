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
                <Card className="h-full p-4 transition-colors hover:border-accent">
                  <p className="font-display text-[15px] font-medium leading-snug text-ink">
                    {project.name}
                  </p>
                  <p className="tabular mt-1 text-xs text-ink-3">
                    {duration(project.duration_sec)} · {relativeTime(project.updated_at)}
                  </p>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    <Badge tone={project.has_video ? "fit" : "default"}>
                      {project.has_video ? "Видео" : "Видеогүй"}
                    </Badge>
                    {project.has_transcript && <Badge tone="fit">Текст</Badge>}
                    {project.has_suggestions && <Badge tone="accent">Санал</Badge>}
                    {project.n_outputs > 0 && (
                      <Badge tone="accent">{project.n_outputs} бэлэн</Badge>
                    )}
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
