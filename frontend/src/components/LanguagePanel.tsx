"use client";

/**
 * Correct the language a video was declared in.
 *
 * It is chosen once, at upload, and a wrong choice is not a detail: it picks
 * the recogniser, and a video heard as the wrong language comes back as
 * confident nonsense — billed. Without a way to fix it here the only remedy
 * was deleting the project and uploading the file again.
 *
 * Saving does NOT re-transcribe. That is a paid run, so the page says it is
 * needed rather than starting it on the producer's behalf.
 */

import { useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { LANGUAGE_LABELS } from "@/lib/format";
import type { SourceLanguage } from "@/lib/types";
import { Alert, Button, Field, Select } from "@/components/ui";

export function LanguagePanel({
  projectId,
  language,
  hasTranscript,
  onSaved,
}: {
  projectId: string;
  language: SourceLanguage;
  /** A transcript made in the old language is what a change leaves wrong. */
  hasTranscript: boolean;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<SourceLanguage>(language);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Kept after the save, when `dirty` is false again: the transcript on the
  // page is still in the old language until someone runs it again.
  const [changed, setChanged] = useState(false);

  const dirty = draft !== language;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.updateProject(projectId, { language: draft });
      setChanged(true);
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <Field
        label="Видеонд ярьж буй хэл"
        hint={
          dirty && hasTranscript
            ? "Одоогийн текст хуучин хэлээр танигдсан — хадгалсны дараа яриаг дахин таних хэрэгтэй."
            : undefined
        }
      >
        <Select
          value={draft}
          onChange={(e) => setDraft(e.target.value as SourceLanguage)}
          disabled={saving}
          className="self-start"
        >
          {(Object.keys(LANGUAGE_LABELS) as SourceLanguage[]).map((code) => (
            <option key={code} value={code}>
              {LANGUAGE_LABELS[code]}
            </option>
          ))}
        </Select>
      </Field>

      {changed && !dirty && hasTranscript && (
        <Alert tone="warn">
          Хэл солигдлоо. Текст таб дээр «Яриаг дахин таних» дарж шинэ хэлээр нь танина уу.
        </Alert>
      )}

      {error && <Alert>{error}</Alert>}

      <Button
        tone="primary"
        onClick={save}
        loading={saving}
        disabled={!dirty}
        className="self-start"
      >
        Хэл хадгалах
      </Button>
    </div>
  );
}
