"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { errorMessage, useAuth } from "@/lib/auth";
import { Alert, Button, Card, Field, TextInput } from "@/components/ui";

export default function LoginPage() {
  const { user, loading, signIn } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!loading && user) router.replace("/");
  }, [loading, user, router]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signIn(username.trim(), password);
      router.replace("/");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-md flex-col justify-center px-6 py-16">
      <h1 className="font-display text-3xl font-semibold tracking-tight">MoniReels</h1>
      <p className="mt-2 text-sm text-ink-2">
        Урт видеог богино хэмжээний видео болгон хувиргах студи.
      </p>

      <Card className="mt-8 p-6">
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Field label="Нэвтрэх нэр">
            <TextInput
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </Field>
          <Field label="Нууц үг">
            <TextInput
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>

          {error && <Alert>{error}</Alert>}

          <Button type="submit" tone="primary" loading={submitting} className="mt-1">
            Нэвтрэх
          </Button>
        </form>
      </Card>

      <p className="mt-6 text-xs text-ink-3">
        Бүртгэл шаардлагатай бол админаас хүсэлт гаргана уу. Энэ системд нээлттэй бүртгэл байхгүй.
      </p>
    </main>
  );
}
