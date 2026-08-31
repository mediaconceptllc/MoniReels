"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuth, errorMessage } from "@/lib/auth";
import type { Capability, ProviderSettings, ProviderSettingsPatch } from "@/lib/types";
import { Alert, Button, Card, Field, Spinner, TextInput } from "@/components/ui";
import { BrandAssetsCard } from "@/components/BrandAssetsCard";
import { CapabilityTable } from "@/components/CapabilityTable";

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
    // One key, two features, and only one of them is built. Said plainly,
    // because a stored key that half the page reads must not look like it
    // powers the other half too.
    hint: "Яриа таних (Scribe) хэсэгт ашиглагдана. Дуу оруулах (TTS) хараахан хэрэгжээгүй.",
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
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [sttProviders, setSttProviders] = useState<string[]>([]);
  const [sttProvider, setSttProvider] = useState<string>("");

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
    try {
      // Separate from the settings read: what each key POWERS is worth
      // showing even when one of the two calls fails.
      const status = await api.providers();
      setCapabilities(status.capabilities);
      setSttProviders(status.stt_providers ?? []);
      setSttProvider(status.stt?.provider ?? "");
    } catch {
      setCapabilities([]);
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
          Гадаад үйлчилгээ ба брэндийн материал. Хадгалмагц дараагийн ажлаас эхлэн хүчинтэй —
          дахин deploy хийх шаардлагагүй.
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
          <div>
            <h2 className="font-display text-lg font-semibold text-ink">API түлхүүрүүд</h2>
            <p className="mt-1 text-sm text-ink-3">
              Хадгалагдсан түлхүүр буцаж уншигдахгүй — сүүлийн 4 тэмдэгт нь л харагдана.
            </p>
          </div>
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

      {capabilities.length > 0 && (
        <CapabilityTable
          capabilities={capabilities}
          sttProvider={sttProvider}
          sttProviders={sttProviders}
          onSttProvider={(name) => {
            // Applied immediately rather than left in the draft with the
            // keys: this is one click, and a recogniser that looks selected
            // but is not saved is how a job runs on the wrong vendor.
            setSttProvider(name);
            void (async () => {
              try {
                await api.saveProviderSettings({ stt_provider: name });
                await load();
              } catch (err) {
                setError(errorMessage(err));
              }
            })();
          }}
        />
      )}

      <BrandAssetsCard />

    </div>
  );
}
