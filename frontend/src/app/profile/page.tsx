"use client";

import { useState } from "react";
import Link from "next/link";
import { api, setToken } from "@/lib/api";
import { useAuth, errorMessage } from "@/lib/auth";
import { Alert, Badge, Button, Card, Field, Spinner, TextInput } from "@/components/ui";
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
        <Spinner />
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
          <p className="mt-1 text-sm text-ink-3">Таны бүртгэл ба нэвтрэх мэдээлэл.</p>
        </div>

        <Card>
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
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-display text-lg font-semibold text-ink">Тохиргоо</h2>
                <p className="mt-1 text-sm text-ink-3">
                  Гадаад үйлчилгээний түлхүүр, брэндийн материал, хадмалын загвар.
                </p>
              </div>
              <Link href="/admin">
                <Button>Нээх</Button>
              </Link>
            </div>
          </Card>
        )}
      </div>
    </Shell>
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
    <Card>
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
