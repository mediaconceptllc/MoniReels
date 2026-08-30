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
import type { ExportSettings } from "@/lib/types";
import { Alert, Badge, Button, Field } from "@/components/ui";

const PRESETS = ["veryfast", "faster", "fast", "medium", "slow"] as const;

const PRESET_LABELS: Record<string, string> = {
  veryfast: "Маш хурдан",
  faster: "Хурдан",
  fast: "Хурдавтар",
  medium: "Дунд",
  slow: "Удаан",
};

export function ExportSettingsPanel({
  projectId,
  settings,
  onSaved,
}: {
  projectId: string;
  settings: ExportSettings;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<ExportSettings>(settings);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings);

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
          <select
            value={draft.orientation}
            onChange={(e) => update("orientation", e.target.value as ExportSettings["orientation"])}
            className="rounded-md border border-rule bg-surface px-3 py-2 text-sm text-ink"
          >
            <option value="portrait">Босоо (1080×1920)</option>
            <option value="landscape">Хэвтээ (1920×1080)</option>
          </select>
        </Field>

        {draft.orientation === "portrait" && (
          <Field label="Хажуугийн зай" hint="Хэвтээ кадрыг босоо хүрээнд яаж багтаах вэ.">
            <select
              value={draft.portrait_fill}
              onChange={(e) =>
                update("portrait_fill", e.target.value as ExportSettings["portrait_fill"])
              }
              className="rounded-md border border-rule bg-surface px-3 py-2 text-sm text-ink"
            >
              <option value="blur">Бүдгэрүүлэх</option>
              <option value="crop">Тайрах</option>
              <option value="pad">Хар зай</option>
            </select>
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
            className="accent-[var(--accent)]"
          />
        </Field>

        <Field
          label="Кодлолтын хурд"
          hint="Удаан нь чанарыг НЭМЭХГҮЙ — ижил чанарыг цөөн битээр багтаана. Сервер дээр GPU байхгүй тул хугацаанд шууд нөлөөлнө."
        >
          <select
            value={draft.preset}
            onChange={(e) => update("preset", e.target.value)}
            className="rounded-md border border-rule bg-surface px-3 py-2 text-sm text-ink"
          >
            {PRESETS.map((preset) => (
              <option key={preset} value={preset}>
                {PRESET_LABELS[preset]}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div className="flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={draft.burn_subtitles}
            onChange={(e) => update("burn_subtitles", e.target.checked)}
          />
          Хадмалыг видеон дээр шатаах
        </label>
        <label className="flex items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={draft.write_srt}
            onChange={(e) => update("write_srt", e.target.checked)}
          />
          .srt файл тусад нь гаргах
        </label>
      </div>

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
