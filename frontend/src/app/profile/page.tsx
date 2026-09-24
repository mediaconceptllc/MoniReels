"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, setToken } from "@/lib/api";
import { useAuth, errorMessage } from "@/lib/auth";
import { duration, relativeTime } from "@/lib/format";
import type { ProjectSummary } from "@/lib/types";
import { Alert, Badge, Button, Card, Field, Loading, Skeleton, TAP, TextInput } from "@/components/ui";
import { Shell } from "@/components/Shell";

const ROLE_LABEL: Record<string, string> = {
  admin: "Админ",
  editor: "Хэрэглэгч",
};

export default function ProfilePage() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <Shell>
        <Loading className="flex max-w-2xl flex-col gap-6">
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-40" />
            <Skeleton className="h-4 w-64" />
          </div>
          <Skeleton className="h-24 rounded-lg" />
          <Skeleton className="h-80 rounded-lg" />
        </Loading>
      </Shell>
    );
  }
  if (!user) {
    return (
      <Shell>
        <Alert>Нэвтэрнэ үү.</Alert>
      </Shell>
    );
  }

  return (
    <Shell>
      <div className="flex max-w-2xl flex-col gap-6">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">Профайл</h1>
          <p className="mt-1 text-sm text-ink-3">Таны бүртгэл ба хийсэн ажил.</p>
        </div>

        {/* The page was a name, a role and a password form — three facts, none
            of them about the work the account has actually done. */}
        <Work />

        <Card className="p-5">
          <dl className="flex flex-col gap-3">
            <Row label="Нэвтрэх нэр" value={user.username} />
            <Row
              label="Эрх"
              value={
                <Badge tone={user.role === "admin" ? "accent" : "default"}>
                  {ROLE_LABEL[user.role] ?? user.role}
                </Badge>
              }
            />
          </dl>
        </Card>

        <PasswordCard />

        {/* Settings live behind the admin role on the SERVER; this link is
            convenience, and its absence is not what keeps anyone out. */}
        {user.role === "admin" && (
          <Card className="p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-display text-lg font-semibold text-ink">Тохиргоо</h2>
                <p className="mt-1 text-sm text-ink-3">
                  Гадаад үйлчилгээний түлхүүр, брэндийн материал, хадмалын загвар.
                </p>
              </div>
              <Link href="/admin" className={`${TAP} inline-flex`}>
                <Button>Нээх</Button>
              </Link>
            </div>
          </Card>
        )}
      </div>
    </Shell>
  );
}

/**
 * What this account has made.
 *
 * Read from the projects list the home page already loads — owner-scoped on
 * the server, so nothing here widens what the account can see, and no new
 * endpoint exists to keep in step with one.
 *
 * The counts describe THOSE projects and say so. The list is capped
 * server-side, and a sum presented as a lifetime total would be quietly short
 * by whatever the cap cut off; "N төслөөс" is both the scope and the number.
 */
function Work() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    try {
      setProjects(await api.listProjects());
    } catch {
      // A profile is not worth an error banner: the name, the role and the
      // password form below are all still usable without this block.
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (failed) return null;
  if (!projects) {
    return (
      <Loading>
        <Skeleton className="h-28 rounded-lg" />
      </Loading>
    );
  }
  if (projects.length === 0) {
    return (
      <Card className="flex flex-col items-start gap-3 p-5">
        <div>
          <h2 className="font-display text-lg font-semibold text-ink">Миний ажил</h2>
          <p className="mt-1 text-sm text-ink-3">Одоогоор төсөл алга.</p>
        </div>
        {/* An empty state with a way out of it. A link buried in the sentence
            would be the one target on this page too small to hit — and making
            THAT 44px tall would break the line it sits in. */}
        <Link href="/" className={`${TAP} inline-flex`}>
          <Button tone="primary">Эхний видеогоо оруулах</Button>
        </Link>
      </Card>
    );
  }

  const outputs = projects.reduce((n, p) => n + p.n_outputs, 0);
  const seconds = projects.reduce((n, p) => n + p.duration_sec, 0);
  const last = Math.max(...projects.map((p) => p.updated_at));

  return (
    <Card className="p-5">
      <h2 className="font-display text-lg font-semibold text-ink">Миний ажил</h2>
      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Stat label="Төсөл" value={String(projects.length)} />
        <Stat label="Бэлэн видео" value={String(outputs)} note={`${projects.length} төслөөс`} />
        <Stat label="Эх материал" value={duration(seconds)} note="хадмалын уртаар ≈" />
        <Stat label="Сүүлд" value={relativeTime(last)} />
      </dl>
    </Card>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-ink-3">{label}</dt>
      <dd className="tabular font-display text-lg font-semibold leading-tight text-ink">
        {value}
      </dd>
      {note && <dd className="text-[11px] text-ink-3">{note}</dd>}
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <dt className="text-sm text-ink-3">{label}</dt>
      <dd className="text-sm text-ink">{value}</dd>
    </div>
  );
}

/**
 * Changing a password.
 *
 * The endpoint has existed since the beginning and nothing ever called it —
 * an account whose password cannot be changed is one nobody can recover from
 * a leak. It returns a fresh token, because a change that left the old one
 * working would be a change that protects nothing.
 */
function PasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch = confirm.length > 0 && next !== confirm;
  const ready = current.length > 0 && next.length >= 12 && !mismatch;

  async function submit() {
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      const { token } = await api.changePassword(current, next);
      // The server invalidates the old token, so the session has to adopt
      // the new one or the very next request signs the user out.
      setToken(token);
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-5">
      <div className="flex flex-col gap-4">
        <div>
          <h2 className="font-display text-lg font-semibold text-ink">Нууц үг солих</h2>
          <p className="mt-1 text-sm text-ink-3">
            Дор хаяж 12 тэмдэгт. Сольсны дараа өмнөх нэвтрэлт хүчингүй болно.
          </p>
        </div>

        {error && <Alert>{error}</Alert>}
        {done && <Alert tone="accent">Нууц үг солигдлоо.</Alert>}

        <Field label="Одоогийн нууц үг">
          <TextInput
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </Field>
        <Field label="Шинэ нууц үг">
          <TextInput
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </Field>
        <Field
          label="Шинэ нууц үг (давтах)"
          hint={mismatch ? "Хоёр нууц үг таарахгүй байна." : undefined}
        >
          <TextInput
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </Field>

        <div>
          <Button tone="primary" onClick={submit} disabled={!ready} loading={busy}>
            Солих
          </Button>
        </div>
      </div>
    </Card>
  );
}
