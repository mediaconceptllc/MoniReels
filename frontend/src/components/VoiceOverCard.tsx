"use client";

/**
 * The voice that reads a Mongolian voice-over.
 *
 * A voice id is twenty letters nobody can tell apart, so it is picked from
 * the account's own list, by name, with ElevenLabs' sample to hear it by —
 * never typed.
 *
 * Whether the model speaks Mongolian is ASKED of ElevenLabs and shown as it
 * answers: yes, no, or "could not ask". The third is not folded into the
 * second, because a warning printed for a model nobody could check is a
 * guess made in the operator's direction.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import type { ProviderSettings, TtsVoice, TtsVoices } from "@/lib/types";
import { Alert, Badge, Button, Card, Field, Loading, Select, Skeleton, TextInput } from "@/components/ui";

function voiceLabel(voice: TtsVoice): string {
  const traits = [voice.gender, voice.accent].filter(Boolean).join(", ");
  return traits ? `${voice.name} · ${traits}` : voice.name;
}

export function VoiceOverCard({
  settings,
  onSaved,
}: {
  settings: ProviderSettings | null;
  onSaved: () => void;
}) {
  const keySet = !!settings?.elevenlabs_api_key.set;
  const [data, setData] = useState<TtsVoices | null>(null);
  const [voiceId, setVoiceId] = useState<string>("");
  const [model, setModel] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      const answer = await api.ttsVoices();
      setData(answer);
      setVoiceId(answer.voice_id ?? "");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    if (keySet) void load();
  }, [keySet, load]);

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await api.saveProviderSettings({
        elevenlabs_tts_voice_id: voiceId,
        ...(model.trim() ? { elevenlabs_tts_model: model.trim() } : {}),
      });
      setModel("");
      setSaved(true);
      onSaved();
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const chosen = data?.voices.find((v) => v.id === voiceId) ?? null;
  // Only an https sample is played: the URL comes from a third party.
  const preview = chosen?.preview_url?.startsWith("https://") ? chosen.preview_url : null;
  const dirty = voiceId !== (data?.voice_id ?? "") || model.trim() !== "";

  return (
    <Card className="p-5">
      <div className="flex flex-col gap-4">
        <div>
          <h2 className="font-display text-lg font-semibold text-ink">Монгол дуу</h2>
          <p className="mt-1 text-sm text-ink-3">
            Монголоос бусад хэлтэй видеоны экспортод орчуулгыг энэ хоолойгоор уншуулна.
            Төсөл бүр Экспортын тохиргоонд асааж, унтраана.
          </p>
        </div>

        {!keySet ? (
          <Alert tone="warn">Эхлээд дээрх ElevenLabs API түлхүүрийг оруулна уу.</Alert>
        ) : !data && !error ? (
          <Loading className="flex flex-col gap-3">
            <Skeleton className="h-4 w-48" />
            <Skeleton className="h-11 w-full max-w-md rounded-md" />
          </Loading>
        ) : (
          <>
            {data?.error && <Alert>{data.error}</Alert>}

            {data && (
              <div className="flex flex-wrap items-center gap-2 text-sm text-ink-2">
                <span>
                  Загвар: <span className="font-mono text-[13px]">{data.model}</span>
                </span>
                {data.mongolian === true && <Badge tone="fit">Монгол хэл жагсаалтад бий</Badge>}
                {data.mongolian === null && !data.error && (
                  <Badge>Монгол хэлний дэмжлэгийг шалгаж чадсангүй</Badge>
                )}
              </div>
            )}
            {data?.mongolian === false && (
              <Alert tone="warn">
                ElevenLabs-ийн {data.model} загварын хэлний жагсаалтад монгол хэл алга. Дуу нь өөр
                хэлний аялгаар гарч магадгүй — эхлээд нэг богино видеогоор туршиж сонсоно уу.
              </Alert>
            )}

            {data && data.voices.length > 0 && (
              <Field
                label="Хоолой"
                hint="Жишээ бичлэг нь ElevenLabs-ийнх, англиар — хоолойн өнгийг сонсоход."
              >
                <Select value={voiceId} onChange={(e) => setVoiceId(e.target.value)}>
                  <option value="">— Сонгоогүй —</option>
                  {data.voices.map((voice) => (
                    <option key={voice.id} value={voice.id}>
                      {voiceLabel(voice)}
                    </option>
                  ))}
                </Select>
              </Field>
            )}
            {preview && (
              <audio key={preview} controls preload="none" src={preview} className="w-full max-w-md" />
            )}

            <Field
              label="ElevenLabs загвар"
              hint="Хоосон үлдээвэл хэвээр. Хоолой эсвэл загвар солигдвол мөр бүрийг шинээр үүсгэж, дахин төлнө."
            >
              <TextInput
                autoComplete="off"
                spellCheck={false}
                placeholder={settings?.elevenlabs_tts_model.hint || "eleven_v3"}
                value={model}
                onChange={(e) => setModel(e.target.value)}
              />
            </Field>

            {error && <Alert>{error}</Alert>}

            <div className="flex items-center gap-3">
              <Button tone="primary" onClick={save} loading={busy} disabled={!dirty}>
                Хадгалах
              </Button>
              {saved && !dirty && <Badge tone="fit">✓ Хадгалагдсан</Badge>}
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
