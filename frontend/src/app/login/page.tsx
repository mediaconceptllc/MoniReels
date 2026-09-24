"use client";

/**
 * The first screen, and for a new operator the only one they have seen.
 *
 * It was a 448px column in the middle of an empty display: a title, one
 * sentence, two fields. Nothing on it said what the product does with a
 * video, how many steps there are, or what comes back at the end — so the
 * first answer to "what is this" arrived only after signing in, if at all.
 *
 * The pipeline is the answer, and it is four steps in a fixed order. They sit
 * beside the form on a wide screen and BELOW it on a narrow one: a returning
 * user wants the password box under their thumb, and they have read the steps
 * already.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { errorMessage, useAuth } from "@/lib/auth";
import { Alert, Button, Card, Field, TextInput } from "@/components/ui";

/** The same four stages the project page walks down, named the same way. A
 *  different list here would be a second description of one pipeline, and the
 *  two would drift the first time a stage changed. */
const STEPS: { title: string; detail: string }[] = [
  {
    title: "Видео оруулах",
    detail: "Урт бичлэгээ хуулна. Файл шууд хадгалах сан руу явна — сервер дундуур дамжихгүй.",
  },
  {
    title: "Яриаг текст болгох",
    detail: "Үгийн нарийвчлалтай хугацаатай хадмал гарна. Буруу үгийг нь сонсож байж засна.",
  },
  {
    title: "Санал авах",
    detail: "Загвар текстээс богино видеоны огтлол ба YouTube хураангуйн төлөвлөгөө санал болгоно.",
  },
  {
    title: "Экспортлох",
    detail: "Сонгосон саналыг босоо хүрээнд, шатаасан хадмалтай, логотойгоор гаргана.",
  },
];

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
    <main className="mx-auto flex min-h-dvh w-full max-w-5xl flex-col justify-center px-6 py-16">
      <div className="grid items-start gap-10 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)] lg:gap-16">
        <div className="flex flex-col">
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
            Бүртгэл шаардлагатай бол админаас хүсэлт гаргана уу. Энэ системд нээлттэй бүртгэл
            байхгүй.
          </p>
        </div>

        {/* Ordered, not decorative: the steps run in this order because each
            one needs what the one before it produced, which is also why the
            project page cannot offer them as a menu. */}
        <section className="flex flex-col gap-5 lg:pt-14">
          <h2 className="font-display text-base font-semibold text-ink">Яаж ажилладаг вэ</h2>
          <ol className="flex flex-col gap-5">
            {STEPS.map((step, index) => (
              <li key={step.title} className="flex gap-3.5">
                <span
                  aria-hidden
                  className="mt-0.5 inline-flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full bg-accent-soft text-[13px] font-semibold text-accent"
                >
                  {index + 1}
                </span>
                <span className="flex flex-col gap-1">
                  <span className="font-display text-[15px] font-medium leading-snug text-ink">
                    {step.title}
                  </span>
                  <span className="text-[13px] leading-relaxed text-ink-3">{step.detail}</span>
                </span>
              </li>
            ))}
          </ol>
          <p className="text-xs text-ink-3">
            Эхний хоёр алхам төлбөртэй гадаад үйлчилгээгээр явна. Товч дарахаас өмнө ойролцоо
            зардлыг нь харуулна.
          </p>
        </section>
      </div>
    </main>
  );
}
