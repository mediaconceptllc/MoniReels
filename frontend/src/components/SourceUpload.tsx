"use client";

/**
 * Give a project its video again (audit UX-6).
 *
 * A project whose upload never arrived — the tab was closed, the network
 * dropped, the file was refused — or whose import failed used to sit at
 * "Видео боловсруулагдаж байна…" for ever: no reason, and no way out but
 * deleting it. This is the way out. When the same file is picked again after
 * an interrupted multipart upload, only the parts R2 does not hold yet are
 * sent (lib/upload.ts keeps the upload id); any other file starts afresh.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { fileSize } from "@/lib/format";
import {
  ACCEPT,
  sameFile,
  savedUpload,
  uploadRefusal,
  uploadSource,
  type SavedUpload,
  type SourceUpload as Transfer,
} from "@/lib/upload";
import { Alert, Button, ProgressBar } from "@/components/ui";

export function SourceUpload({
  projectId,
  label,
  onUploaded,
}: {
  projectId: string;
  /** The button: «Дахин хуулах» after a failure, «Видео хуулах» otherwise. */
  label: string;
  onUploaded: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const transfer = useRef<Transfer | null>(null);
  const [saved, setSaved] = useState<SavedUpload | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Read after mount: localStorage is not there during the server render.
  useEffect(() => {
    setSaved(savedUpload(projectId));
  }, [projectId]);

  const uploading = progress !== null;

  useEffect(() => {
    if (!uploading) return;
    const onUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [uploading]);

  async function send(chosen: File) {
    const problem = uploadRefusal(chosen);
    if (problem) {
      setError(problem);
      return;
    }
    setFile(chosen);
    setError(null);
    setProgress(0);
    try {
      // The same file as an interrupted upload: carry on with the pending
      // upload. Anything else: a new source, the old one cleared away.
      const resume = saved !== null && sameFile(saved, chosen);
      let fresh: string | null = null;
      if (!resume) fresh = (await api.reupload(projectId, chosen.name, chosen.size)).upload_url;
      const upload = uploadSource(
        projectId,
        chosen,
        async () => {
          if (fresh) {
            const url = fresh;
            fresh = null;
            return url;
          }
          return (await api.reupload(projectId, chosen.name, chosen.size)).upload_url;
        },
        setProgress,
      );
      transfer.current = upload;
      await upload.promise;
      await api.uploadComplete(projectId);
      setSaved(null);
      onUploaded();
    } catch (err) {
      setError(errorMessage(err));
      setSaved(savedUpload(projectId));
    } finally {
      transfer.current = null;
      setProgress(null);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(e) => {
          const chosen = e.target.files?.[0];
          e.target.value = "";
          if (chosen) void send(chosen);
        }}
      />
      {saved && !uploading && (
        <p className="text-sm text-ink-2">
          Өмнөх хуулалт дуусаагүй байна ({saved.name}, {fileSize(saved.size)}). Яг тэр файлыг
          сонговол хуулагдсан хэсгүүдийг дахин илгээхгүй, үлдсэнийг нь л хуулна. Өөр файл сонговол
          шинээр хуулна.
        </p>
      )}
      {uploading && file ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="text-ink-2">Хуулж байна</span>
            <span className="tabular text-ink-3">
              {fileSize(Math.round((progress ?? 0) * file.size))} / {fileSize(file.size)} ·{" "}
              {Math.round((progress ?? 0) * 100)}%
            </span>
          </div>
          <ProgressBar value={progress ?? 0} label="Видео хуулж байна" />
          <Button tone="quiet" className="self-start" onClick={() => transfer.current?.pause()}>
            Зогсоох
          </Button>
        </div>
      ) : (
        <Button tone="primary" className="self-start" onClick={() => inputRef.current?.click()}>
          {saved ? "Хуулалтыг үргэлжлүүлэх" : label}
        </Button>
      )}
      {error && <Alert>{error}</Alert>}
    </div>
  );
}
