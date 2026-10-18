"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { errorMessage, useRequireAuth } from "@/lib/auth";
import type { BalanceRow, Capability, ProviderSettings, ProviderSettingsPatch, SttStatus } from "@/lib/types";
import { Alert, Badge, Button, Card, Field, Loading, Skeleton, TAP, TextInput } from "@/components/ui";
import { AdminJobs } from "@/components/AdminJobs";
import { AdminUsers } from "@/components/AdminUsers";
import { BrandAssetsCard } from "@/components/BrandAssetsCard";
import { VoiceOverCard } from "@/components/VoiceOverCard";
import { Shell } from "@/components/Shell";
import { CapabilityTable } from "@/components/CapabilityTable";

type SecretName = "openrouter_api_key" | "duudlaga_api_key" | "elevenlabs_api_key";

/**
 * Which capability each key actually switches on.
 *
 * `stt` is the one that cannot be read off the key alone: two recognisers can
 * both hold a key and only the SELECTED one runs anything, so a key for the
 * other must not report "working" — that is exactly the state an operator
 * comes to this page to untangle.
 */
const SECRETS: {
  name: SecretName;
  label: string;
  hint: string;
  capability: Capability["name"];
  /** Set when this key belongs to one recogniser among several. */
  sttProvider?: string;
}[] = [
  {
    name: "openrouter_api_key",
    label: "OpenRouter API түлхүүр",
    hint: "Санал боловсруулах бүх ажил үүгээр явна.",
    capability: "llm",
  },
  {
    name: "duudlaga_api_key",
    label: "duudlaga.dev API түлхүүр",
    hint: "Яриаг текст болгоход хэрэглэгдэнэ. Түлхүүр дээрээ өдрийн зарлагын хязгаар тавихыг зөвлөж байна.",
    capability: "stt",
    sttProvider: "duudlaga",
  },
  {
    name: "elevenlabs_api_key",
    label: "ElevenLabs API түлхүүр",
    // One key, two features. The badge follows the voice-over, which needs
    // a chosen voice besides the key; the English recogniser needs only the
    // key, and the capability table says so on its own row.
    hint: "Англи яриаг таних (Scribe) ба монгол дуу (доорх хоолой) хоёуланд хэрэглэгдэнэ.",
    capability: "tts",
  },
];

/** The live state of what this key switches on, beside the key itself.
 *
 *  The table further down already said all of this, and the operator who came
 *  here because something is not working had to read the field, scroll, find
 *  the matching row, and carry one back to the other. */
function KeyState({
  secret,
  capabilities,
  sttProvider,
}: {
  secret: (typeof SECRETS)[number];
  capabilities: Capability[];
  sttProvider: string;
}) {
  // Nothing is claimed when the status read failed — the keys are still
  // editable, and a badge invented from no data is worse than no badge.
  if (capabilities.length === 0) return null;

  if (secret.sttProvider && sttProvider && secret.sttProvider !== sttProvider) {
    return <Badge>Сонгогдоогүй</Badge>;
  }
  const capability = capabilities.find((c) => c.name === secret.capability);
  if (!capability) return null;
  if (capability.ready) return <Badge tone="fit">Ажиллаж байна</Badge>;
  return <Badge tone="warn">Тохируулаагүй</Badge>;
}

function sourceLabel(field: ProviderSettings[SecretName] | undefined): string {
  if (!field?.set) return "Тавигдаагүй";
  return field.source === "db" ? `Энэ хуудсаас · ${field.hint}` : `Орчны хувьсагчаас · ${field.hint}`;
}

/** A balance value as the provider's own unit gives it. A `…_unix` field is
 *  a moment, and a ten-digit number is not how anyone reads a date. */
function balanceValue(row: BalanceRow): string {
  if (typeof row.value === "number") {
    if (row.name.endsWith("_unix")) return new Date(row.value * 1000).toLocaleString("mn-MN");
    return row.value.toLocaleString("mn-MN");
  }
  if (typeof row.value === "boolean") return row.value ? "Тийм" : "Үгүй";
  return row.value;
}

/** What the selected recogniser says it has left.
 *
 *  The server asked for this on every visit and the page never showed it,
 *  so "out of credits" was still discovered the expensive way: by a
 *  transcription that claimed a worker and downloaded the video first. */
function SttBalanceCard({ stt }: { stt: SttStatus }) {
  return (
    <Card className="p-5">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-display text-lg font-semibold text-ink">Яриа таних — үлдэгдэл</h2>
          <Badge>{stt.provider}</Badge>
          {stt.ok === true && <Badge tone="fit">Холбогдсон</Badge>}
        </div>
        {!stt.configured ? (
          <p className="text-sm text-ink-3">Түлхүүр тавигдаагүй тул асуух боломжгүй.</p>
        ) : stt.ok === false ? (
          <Alert tone="warn">{stt.error || "Үйлчилгээ хариу өгсөнгүй."}</Alert>
        ) : stt.balance && stt.balance.length > 0 ? (
          <dl className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
            {stt.balance.map((row) => (
              <div key={row.name} className="flex flex-wrap justify-between gap-x-3 border-b border-rule-soft py-1">
                <dt className="font-mono text-xs text-ink-3 [overflow-wrap:anywhere]">{row.name}</dt>
                <dd className="tabular text-sm text-ink">{balanceValue(row)}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-sm text-ink-3">Үйлчилгээ хариулсан ч үлдэгдлийн мэдээлэл өгсөнгүй.</p>
        )}
      </div>
    </Card>
  );
}

type Tab = "settings" | "users" | "jobs";
const TABS: { id: Tab; label: string }[] = [
  { id: "settings", label: "Үйлчилгээ" },
  { id: "users", label: "Хэрэглэгчид" },
  { id: "jobs", label: "Ажлууд" },
];

/** The tab is kept in the address, so a reload or a link lands on it. A hash
 *  rather than a query: this page is static, and reading the query needs a
 *  Suspense boundary it does not have. */
function tabFromHash(): Tab {
  if (typeof window === "undefined") return "settings";
  const hash = window.location.hash.slice(1);
  return TABS.some((t) => t.id === hash) ? (hash as Tab) : "settings";
}

export default function AdminPage() {
  // useRequireAuth, not useAuth: a signed-out visitor to /admin used to sit
  // on a blank page forever — the role guard below only redirects someone
  // who IS signed in, so nobody ever sent the signed-out case anywhere.
  const { user, loading: authLoading } = useRequireAuth();
  const router = useRouter();

  const [settings, setSettings] = useState<ProviderSettings | null>(null);
  const [drafts, setDrafts] = useState<ProviderSettingsPatch>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string[] | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [sttProviders, setSttProviders] = useState<string[]>([]);
  const [sttProvider, setSttProvider] = useState<string>("");
  const [stt, setStt] = useState<SttStatus | null>(null);
  const [tab, setTab] = useState<Tab>("settings");

  useEffect(() => {
    setTab(tabFromHash());
  }, []);

  function openTab(next: Tab) {
    setTab(next);
    window.history.replaceState(null, "", next === "settings" ? window.location.pathname : `#${next}`);
  }

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
      setStt(status.stt ?? null);
    } catch {
      setCapabilities([]);
      setStt(null);
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

  // One wait, three reasons, and the reader cannot act on the difference:
  // the session is loading, a redirect is in flight, or the settings are on
  // their way. All three now happen INSIDE the shell, so the header and the
  // page width do not appear and disappear around them.
  if (authLoading || user?.role !== "admin" || (!settings && !error)) {
    return (
      <Shell>
        <Loading className="flex flex-col gap-6">
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-40" />
            <Skeleton className="h-4 w-full max-w-xl" />
          </div>
          {/* One block per card the page settles into: capabilities, keys,
              the voice, brand assets. The heights are approximate — what has to match is
              the COUNT and the order, so the operator's eye is already on the
              card they came for. */}
          <Skeleton className="h-96 rounded-lg" />
          <Skeleton className="h-64 rounded-lg" />
          <Skeleton className="h-56 rounded-lg" />
          <Skeleton className="h-72 rounded-lg" />
        </Loading>
      </Shell>
    );
  }

  const dirty = Object.keys(drafts).length > 0;

  return (
    <Shell>
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">Тохиргоо</h1>
          <p className="mt-1 text-sm text-ink-3">
            Гадаад үйлчилгээ, брэндийн материал, хэрэглэгчид ба ажлын дараалал.
          </p>
        </div>

        <div role="tablist" aria-label="Тохиргооны хэсэг" className="flex flex-wrap gap-1 border-b border-rule">
          {TABS.map((t) => (
            <button
              key={t.id}
              id={`admin-tab-${t.id}`}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              aria-controls="admin-tabpanel"
              onClick={() => openTab(t.id)}
              className={`${TAP} -mb-px border-b-2 px-3 text-sm font-medium transition-colors ${
                tab === t.id ? "border-accent text-ink" : "border-transparent text-ink-3 hover:text-ink"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div
          id="admin-tabpanel"
          role="tabpanel"
          aria-labelledby={`admin-tab-${tab}`}
          className="flex flex-col gap-6"
        >
          {tab === "users" && user && <AdminUsers meId={user.id} />}
          {tab === "jobs" && <AdminJobs />}

          {tab === "settings" && (
            <>
              <p className="text-sm text-ink-3">
                Хадгалмагц дараагийн ажлаас эхлэн хүчинтэй — дахин deploy хийх шаардлагагүй.
              </p>

              {error && <Alert>{error}</Alert>}
              {saved && (
                <Alert tone="accent">
                  {saved.length === 0
                    ? "Өөрчлөлт алга — утга нь хэвээрээ байна."
                    : `Хадгаллаа: ${saved.length} талбар шинэчлэгдлээ.`}
                </Alert>
              )}

              {/* What is running comes FIRST. This page is opened far more often
                  to answer "why did that fail" than to type a new key, and the
                  answer used to be below three password fields. */}
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

              {stt && <SttBalanceCard stt={stt} />}

              <Card className="p-5">
                <div className="flex flex-col gap-5">
                  <div>
                    <h2 className="font-display text-lg font-semibold text-ink">API түлхүүрүүд</h2>
                    <p className="mt-1 text-sm text-ink-3">
                      Хадгалагдсан түлхүүр буцаж уншигдахгүй — сүүлийн 4 тэмдэгт нь л харагдана.
                    </p>
                  </div>
                  {SECRETS.map((secret) => (
                    <Field
                      key={secret.name}
                      label={secret.label}
                      hint={secret.hint}
                      aside={
                        <KeyState
                          secret={secret}
                          capabilities={capabilities}
                          sttProvider={sttProvider}
                        />
                      }
                    >
                      <TextInput
                        type="password"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder={sourceLabel(settings?.[secret.name])}
                        value={drafts[secret.name] ?? ""}
                        onChange={(e) => setDrafts((d) => ({ ...d, [secret.name]: e.target.value }))}
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

              <VoiceOverCard settings={settings} onSaved={() => void load()} />

              <BrandAssetsCard />
            </>
          )}
        </div>
      </div>
    </Shell>
  );
}
