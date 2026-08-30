"use client";

/**
 * Create a project and upload its source.
 *
 * Three steps, and the order matters: the API creates the row and hands back
 * a presigned URL, the browser PUTs the file straight to storage, and only
 * then is the import queued. The API is told the upload finished rather than
 * assuming it — a PUT that failed client-side would otherwise produce a job
 * that downloads nothing and fails with a storage error nobody can read.
 */

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { fileSize } from "@/lib/format";
import { uploadToStorage } from "@/lib/upload";
import { Alert, Button, Card, Field, ProgressBar, TextInput } from "@/components/ui";

const ACCEPT = ".mp4,.mov,.mkv,.webm,.m4v,.avi";

export function NewProject({ onCreated }: { onCreated: () => void }) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<(() => void) | null>(null);

  function pick(selected: File | null) {
    setFile(selected);
    setError(null);
    // Default the project name to the filename without its extension — very
    // often exactly what the user would have typed.
    if (selected && !name) setName(selected.name.replace(/\.[^.]+$/, ""));
  }

  async function start() {
    if (!file) return;
    setError(null);
    setProgress(0);
    try {
      const created = await api.createProject(name.trim() || file.name, file.name, file.size);

      const upload = uploadToStorage(created.upload_url, file, setProgress);
      abortRef.current = upload.abort;
      await upload.promise;

      await api.uploadComplete(created.project_id);
      onCreated();
      router.push(`/projects/${created.project_id}`);
    } catch (err) {
      setError(errorMessage(err));
      setProgress(null);
    } finally {
      abortRef.current = null;
    }
  }

  const uploading = progress !== null;

  return (
    <Card className="p-5">
      <h2 className="font-display text-base font-semibold">Шинэ төсөл</h2>
      <p className="mt-1 text-sm text-ink-3">
        Видеог сонгоход шууд хадгалах сан руу хуулагдана — сервер дундуур дамжихгүй.
      </p>

      <div className="mt-4 flex flex-col gap-4">
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="sr-only"
          onChange={(e) => pick(e.target.files?.[0] ?? null)}
        />

        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => inputRef.current?.click()} disabled={uploading}>
            Видео сонгох
          </Button>
          {file ? (
            <span className="text-sm text-ink-2">
              {file.name} <span className="tabular text-ink-3">· {fileSize(file.size)}</span>
            </span>
          ) : (
            <span className="text-sm text-ink-3">Файл сонгоогүй байна</span>
          )}
        </div>

        {file && (
          <Field label="Төслийн нэр">
            <TextInput
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={uploading}
              maxLength={200}
            />
          </Field>
        )}

        {uploading && (
          <div className="flex flex-col gap-2">
            <div className="flex items-baseline justify-between text-sm">
              <span className="text-ink-2">Хуулж байна</span>
              <span className="tabular text-ink-3">{Math.round((progress ?? 0) * 100)}%</span>
            </div>
            <ProgressBar value={progress ?? 0} />
            <Button
              tone="quiet"
              className="self-start px-2 py-1 text-xs"
              onClick={() => abortRef.current?.()}
            >
              Цуцлах
            </Button>
          </div>
        )}

        {error && <Alert>{error}</Alert>}

        {file && !uploading && (
          <Button tone="primary" onClick={start} className="self-start">
            Хуулж эхлэх
          </Button>
        )}
      </div>
    </Card>
  );
}
