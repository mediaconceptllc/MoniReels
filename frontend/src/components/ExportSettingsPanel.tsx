"use client";

/**
 * Render options.
 *
 * `crf` and `preset` matter more here than they did on a desktop: there is no
 * GPU on the server, so H.264 is encoded in software and these two are the
 * only levers over how long an export takes and how large it comes out. The
 * copy says so rather than presenting them as neutral numbers.
 */

import { useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { LANGUAGE_LABELS } from "@/lib/format";
import type { ExportSettings, LogoPosition, SourceLanguage, SpeakerSummary } from "@/lib/types";
import { SpeakerVoices } from "@/components/SpeakerVoices";

// Corners only: a mark anywhere else is a watermark over the face the
// short is about. Top by default — subtitles sit at the bottom.
const LOGO_POSITIONS: [LogoPosition, string][] = [
  ["top-right", "Баруун дээд"],
  ["top-left", "Зүүн дээд"],
  ["bottom-right", "Баруун доод"],
  ["bottom-left", "Зүүн доод"],
];
import { Alert, Badge, Button, Checkbox, Field, Select, TAP } from "@/components/ui";

const PRESETS = ["veryfast", "faster", "fast", "medium", "slow"] as const;

const PRESET_LABELS: Record<string, string> = {
  veryfast: "Маш хурдан",
  faster: "Хурдан",
  fast: "Хурдавтар",
  medium: "Дунд",
  slow: "Удаан",
};

/** Key order is not content. The server keeps the document in Postgres
 *  JSONB, which reorders an object's keys — so `speaker_voices` comes back
 *  in ITS order, not in the order the producer chose voices, and a plain
 *  JSON comparison would call the saved settings unsaved forever. */
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.keys(record)
        .sort()
        .map((key) => [key, canonical(record[key])]),
    );
  }
  return value;
}

export function ExportSettingsPanel({
  projectId,
  settings,
  sourceLanguage,
  speakers,
  hasTranscript,
  onSaved,
}: {
  projectId: string;
  settings: ExportSettings;
  /** What the video is spoken in when that is not Mongolian — the one case
   *  with a choice of subtitle language. Null hides the choice. */
  sourceLanguage: SourceLanguage | null;
  /** Who speaks, for a voice each — the server's view of the transcript. */
  speakers: SpeakerSummary[];
  hasTranscript: boolean;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<ExportSettings>(settings);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const dirty = JSON.stringify(canonical(draft)) !== JSON.stringify(canonical(settings));

  function update<K extends keyof ExportSettings>(key: K, value: ExportSettings[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      // Only this section is sent — a whole-document save would overwrite the
      // transcript edits another tab may have made since this page loaded.
      await api.updateProject(projectId, { export: draft });
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Чиглэл" hint="Reels, Shorts бол босоо.">
          <Select
            value={draft.orientation}
            onChange={(e) => update("orientation", e.target.value as ExportSettings["orientation"])}
          >
            <option value="portrait">Босоо (1080×1920)</option>
            <option value="landscape">Хэвтээ (1920×1080)</option>
          </Select>
        </Field>

        {draft.orientation === "portrait" && (
          <Field label="Хажуугийн зай" hint="Хэвтээ кадрыг босоо хүрээнд яаж багтаах вэ.">
            <Select
              value={draft.portrait_fill}
              onChange={(e) =>
                update("portrait_fill", e.target.value as ExportSettings["portrait_fill"])
              }
            >
              <option value="blur">Бүдгэрүүлэх</option>
              <option value="crop">Тайрах</option>
              <option value="pad">Хар зай</option>
            </Select>
          </Field>
        )}

        <Field
          label={`Чанар (CRF ${draft.crf})`}
          hint="Тоо бага байх тусам чанар өндөр, файл том. 18–23 хооронд байх нь ердийн."
        >
          <input
            type="range"
            min={14}
            max={32}
            value={draft.crf}
            onChange={(e) => update("crf", Number(e.target.value))}
            className={`${TAP} w-full accent-[var(--accent)]`}
          />
        </Field>

        <Field
          label="Кодлолтын хурд"
          hint="Удаан нь чанарыг НЭМЭХГҮЙ — ижил чанарыг цөөн битээр багтаана. Сервер дээр GPU байхгүй тул хугацаанд шууд нөлөөлнө."
        >
          <Select value={draft.preset} onChange={(e) => update("preset", e.target.value)}>
            {PRESETS.map((preset) => (
              <option key={preset} value={preset}>
                {PRESET_LABELS[preset]}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <div className="flex flex-wrap gap-x-5 gap-y-1">
        <Checkbox checked={draft.burn_subtitles} onChange={(on) => update("burn_subtitles", on)}>
          Хадмалыг видеон дээр шатаах
        </Checkbox>
        <Checkbox checked={draft.write_srt} onChange={(on) => update("write_srt", on)}>
          .srt файл тусад нь гаргах
        </Checkbox>
        <Checkbox
          checked={draft.logo.enabled}
          onChange={(on) => update("logo", { ...draft.logo, enabled: on })}
        >
          Лого тавих
        </Checkbox>
        <Checkbox checked={draft.use_intro} onChange={(on) => update("use_intro", on)}>
          Эхлэлийн видео залгах
        </Checkbox>
        <Checkbox checked={draft.use_outro} onChange={(on) => update("use_outro", on)}>
          Төгсгөлийн видео залгах
        </Checkbox>
      </div>

      {sourceLanguage && (draft.burn_subtitles || draft.write_srt) && (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Хадмалын хэл"
            hint={
              draft.subtitle_language === "mn"
                ? "Орчуулагдаагүй мөр үлдсэн бол экспорт эхлэхгүй — эх хэлээр нь гаргахгүй."
                : "Яриаг ярьсан хэлээр нь гаргана. Орчуулга шаардахгүй."
            }
          >
            <Select
              value={draft.subtitle_language}
              onChange={(e) =>
                update("subtitle_language", e.target.value as ExportSettings["subtitle_language"])
              }
            >
              <option value="mn">Монгол — орчуулгаар</option>
              <option value="source">{LANGUAGE_LABELS[sourceLanguage]} — ярьсан хэлээр</option>
            </Select>
          </Field>
        </div>
      )}

      {/* Only for a video not in Mongolian — a Mongolian one already speaks
          it. The price is said here, where the choice is made, not after the
          export: every line read is billed by the character. */}
      {sourceLanguage && (
        <div className="flex flex-col gap-2">
          <Checkbox checked={draft.voice_over} onChange={(on) => update("voice_over", on)}>
            Орчуулгыг монгол дуугаар уншуулах
          </Checkbox>
          <p className="text-xs text-ink-3">
            ElevenLabs-ээр, тэмдэгтээр нь төлбөртэй. Зөвхөн экспортлох хэсгүүдийн мөрийг, нэг
            удаа: дахин экспортлоход өмнө нь үүсгэснээ ашиглана.
          </p>
          {draft.voice_over && (
            <Field
              label={`Эх дууны түвшин — ${Math.round(draft.original_volume * 100)}%`}
              hint="Монгол дууны доор эх дуу хэр сонсогдох вэ. 0% бол бүрэн чимээгүй."
            >
              <input
                type="range"
                min={0}
                max={60}
                step={5}
                value={Math.round(draft.original_volume * 100)}
                onChange={(e) => update("original_volume", Number(e.target.value) / 100)}
                className={`${TAP} w-full accent-[var(--accent)]`}
              />
            </Field>
          )}
          {draft.voice_over && (
            <SpeakerVoices
              speakers={speakers}
              hasTranscript={hasTranscript}
              value={draft.speaker_voices}
              onChange={(next) => update("speaker_voices", next)}
            />
          )}
        </div>
      )}

      {(draft.use_intro || draft.use_outro) && (
        <p className="text-xs text-ink-3">
          Эхлэл/төгсгөлийн видеог админ ⚙️ Тохиргооноос оруулна. Нягтралт, кадрын давтамжийг нь
          систем экспортод тааруулж, шилжилтийг өөрөө тавина. Хадмал тэдгээр дээр гарахгүй.
        </p>
      )}

      {draft.logo.enabled && (
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Логоны байрлал">
            <Select
              value={draft.logo.position}
              onChange={(e) =>
                update("logo", { ...draft.logo, position: e.target.value as LogoPosition })
              }
            >
              {LOGO_POSITIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={`Өргөн — кадрын ${draft.logo.width_pct}%`}>
            <input
              type="range"
              min={4}
              max={40}
              step={1}
              value={draft.logo.width_pct}
              onChange={(e) =>
                update("logo", { ...draft.logo, width_pct: Number(e.target.value) })
              }
              className={`${TAP} w-full accent-[var(--accent)]`}
            />
          </Field>
          <Field label={`Тунгалаг — ${Math.round(draft.logo.opacity * 100)}%`}>
            <input
              type="range"
              min={20}
              max={100}
              step={5}
              value={Math.round(draft.logo.opacity * 100)}
              onChange={(e) =>
                update("logo", { ...draft.logo, opacity: Number(e.target.value) / 100 })
              }
              className={`${TAP} w-full accent-[var(--accent)]`}
            />
          </Field>
          <p className="text-xs text-ink-3 sm:col-span-3">
            Логоны зургийг админ ⚙️ Тохиргооноос нэг удаа оруулна — студид нэг лого.
            Энд зөвхөн энэ төсөлд хэрхэн тавихыг сонгоно.
          </p>
        </div>
      )}

      {error && <Alert>{error}</Alert>}

      <div className="flex items-center gap-3">
        <Button tone="primary" onClick={save} loading={saving} disabled={!dirty}>
          Тохиргоо хадгалах
        </Button>
        {saved && !dirty && <Badge tone="fit">✓ Хадгалагдсан</Badge>}
      </div>
    </div>
  );
}
