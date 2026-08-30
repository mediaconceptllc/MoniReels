"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuth, errorMessage } from "@/lib/auth";
import type { ProviderSettings, ProviderSettingsPatch } from "@/lib/types";
import { Alert, Button, Card, Field, Spinner, TextInput } from "@/components/ui";
import { BrandLogoCard } from "@/components/BrandLogoCard";

type SecretName = "openrouter_api_key" | "duudlaga_api_key" | "elevenlabs_api_key";

const SECRETS: { name: SecretName; label: string; hint: string }[] = [
  {
    name: "openrouter_api_key",
    label: "OpenRouter API түлхүүр",
    hint: "Санал боловсруулах бүх ажил үүгээр явна.",
  },
  {
    name: "duudlaga_api_key",
    label: "duudlaga.dev API түлхүүр",
    hint: "Яриаг текст болгоход хэрэглэгдэнэ. Түлхүүр дээрээ өдрийн зарлагын хязгаар тавихыг зөвлөж байна.",
  },
  {
    name: "elevenlabs_api_key",
    label: "ElevenLabs API түлхүүр",
    // Said plainly: a stored key that nothing reads must not look like a
    // working feature, or the first TTS attempt becomes a bug report.
    hint: "Хадгалагдана — гэхдээ дуу оруулах (TTS) хэсэг хараахан хэрэгжээгүй тул одоогоор ашиглагдахгүй.",
  },
];

function sourceLabel(field: ProviderSettings[SecretName] | undefined): string {
  if (!field?.set) return "Тавигдаагүй";
  return field.source === "db" ? `Энэ хуудсаас · ${field.hint}` : `Орчны хувьсагчаас · ${field.hint}`;
}

export default function AdminPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();

  const [settings, setSettings] = useState<ProviderSettings | null>(null);
  const [drafts, setDrafts] = useState<ProviderSettingsPatch>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string[] | null>(null);

  // The router guard is convenience, not protection: /admin/* is closed on
  // the server, so a non-admin who types the URL gets 403s either way.
  useEffect(() => {
    if (!authLoading && user && user.role !== "admin") router.replace("/");
  }, [authLoading, user, router]);

  const load = useCallback(async () => {
    try {
      setSettings(await api.providerSettings());
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    if (user?.role === "admin") void load();
  }, [user, load]);

  async function save() {
    // Only what was typed. Sending the whole form would blank every field
    // the operator did not touch, because a masked hint is not a value.
    const patch: ProviderSettingsPatch = {};
    for (const [name, value] of Object.entries(drafts)) {
      if (value !== undefined) patch[name as keyof ProviderSettings] = value;
    }
    if (Object.keys(patch).length === 0) return;

    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const result = await api.saveProviderSettings(patch);
      setSettings(result.settings);
      setSaved(result.changed);
      setDrafts({});
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (authLoading || (user?.role === "admin" && !settings && !error)) return <Spinner />;
  if (user?.role !== "admin") return null;

  const dirty = Object.keys(drafts).length > 0;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">Тохиргоо</h1>
        <p className="mt-1 text-sm text-ink-3">
          Гадаад үйлчилгээний түлхүүрүүд. Хадгалмагц дараагийн ажлаас эхлэн хүчинтэй — дахин deploy
          хийх шаардлагагүй.
        </p>
      </div>

      {error && <Alert>{error}</Alert>}
      {saved && (
        <Alert tone="accent">
          {saved.length === 0
            ? "Өөрчлөлт алга — утга нь хэвээрээ байна."
            : `Хадгаллаа: ${saved.length} талбар шинэчлэгдлээ.`}
        </Alert>
      )}

      <Card>
        <div className="flex flex-col gap-5">
          {SECRETS.map(({ name, label, hint }) => (
            <Field key={name} label={label} hint={hint}>
              <TextInput
                type="password"
                autoComplete="off"
                spellCheck={false}
                placeholder={sourceLabel(settings?.[name])}
                value={drafts[name] ?? ""}
                onChange={(e) => setDrafts((d) => ({ ...d, [name]: e.target.value }))}
              />
            </Field>
          ))}

          <Field
            label="OpenRouter модел"
            hint="Жишээ нь anthropic/claude-sonnet-4.5. Хоосон үлдээвэл серверийн анхдагч."
          >
            <TextInput
              autoComplete="off"
              spellCheck={false}
              placeholder={settings?.openrouter_model.hint || "anthropic/claude-sonnet-4.5"}
              value={drafts.openrouter_model ?? ""}
              onChange={(e) => setDrafts((d) => ({ ...d, openrouter_model: e.target.value }))}
            />
          </Field>

          <div className="flex items-center gap-3">
            <Button onClick={save} disabled={busy || !dirty}>
              {busy ? "Хадгалж байна…" : "Хадгалах"}
            </Button>
            <span className="text-xs text-ink-3">
              Талбарыг хоосон үлдээвэл хэвээрээ. Бичсэнээ бүрэн арилгаад хадгалбал тэр түлхүүр
              устаж, серверийн орчны хувьсагч руу буцна.
            </span>
          </div>
        </div>
      </Card>

      <BrandLogoCard />

      <p className="text-xs text-ink-3">
        Хадгалагдсан түлхүүр буцаж уншигдахгүй — сүүлийн 4 тэмдэгт нь л харагдана. Мартвал шинээр
        оруулна.
      </p>
    </div>
  );
}
