"use client";

/**
 * Create a project and upload its source.
 *
 * Three steps, and the order matters: the API creates the row and hands back
 * a presigned URL, the browser sends the file straight to storage (in parts
 * when it is large — lib/upload.ts), and only then is the import queued. The
 * API is told the upload finished rather than assuming it — an upload that
 * failed client-side would otherwise produce a job that downloads nothing and
 * fails with a storage error nobody can read.
 *
 * Three gaps the audit found here (UX-7) are closed: closing the tab during
 * an upload now asks first; an upload that finishes after the producer has
 * moved on no longer drags them into the project (it says so here instead);
 * and cancelling deletes the project it created instead of leaving an empty
 * one behind. A failed upload keeps its project and can be resumed — the
 * parts already stored are not sent again.
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { fileSize, LANGUAGE_LABELS } from "@/lib/format";
import type { SourceLanguage } from "@/lib/types";
import { ACCEPT, uploadRefusal, uploadSource, type SourceUpload } from "@/lib/upload";
import { Alert, Button, Card, Field, ProgressBar, Select, TAP, TextInput } from "@/components/ui";

/** The project an upload is for, kept across a failed attempt so "resume"
 *  sends the rest to the same project instead of creating another. */
interface Target {
  projectId: string;
  name: string;
  /** The PUT the project was created with — good for one hour, once. */
  uploadUrl: string | null;
}

export function NewProject({ onCreated }: { onCreated: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  // Asked, never guessed from the file: it decides which recogniser hears
  // the video, and a wrong one comes back as confident nonsense, billed.
  const [language, setLanguage] = useState<SourceLanguage>("mn");
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ projectId: string; name: string } | null>(null);
  const [canceled, setCanceled] = useState(false);
  const target = useRef<Target | null>(null);
  // Mirrors `target` for rendering: a failed upload offers "resume".
  const [resumable, setResumable] = useState(false);
  const transfer = useRef<SourceUpload | null>(null);
  const cancelling = useRef(false);

  function aim(next: Target | null) {
    target.current = next;
    setResumable(next !== null);
  }

  const uploading = progress !== null;

  // Closing or reloading the tab mid-upload asks first: the browser's own
  // prompt is the only thing that can stop it. Moving to another page of the
  // app does not need to — the upload carries on, and says when it is done.
  useEffect(() => {
    if (!uploading) return;
    const onUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Chrome before 119 still needs the legacy property set.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [uploading]);

  function pick(selected: File | null) {
    const problem = selected ? uploadRefusal(selected) : null;
    if (problem) {
      setFile(null);
      setError(problem);
      return;
    }
    setFile(selected);
    setError(null);
    setNotice(null);
    setCanceled(false);
    // A different file after a failed upload: the project made for the
    // first one holds nothing, and would otherwise be left behind.
    const abandoned = target.current?.projectId;
    if (abandoned) void api.deleteProject(abandoned).catch(() => undefined);
    aim(null);
    // Default the project name to the filename without its extension — very
    // often exactly what the user would have typed.
    if (selected && !name) setName(selected.name.replace(/\.[^.]+$/, ""));
  }

  async function start() {
    if (!file) return;
    setError(null);
    setNotice(null);
    setCanceled(false);
    setProgress(0);
    cancelling.current = false;
    try {
      if (!target.current) {
        const label = name.trim() || file.name;
        const created = await api.createProject(label, file.name, file.size, language);
        if (cancelling.current) {
          // Cancelled while the project was being made.
          await api.deleteProject(created.project_id).catch(() => undefined);
          return;
        }
        aim({ projectId: created.project_id, name: label, uploadUrl: created.upload_url });
      }
      const projectId = target.current!.projectId;
      const upload = uploadSource(
        projectId,
        file,
        async () => {
          // The first single PUT uses the URL from creation; a retry asks for
          // a fresh one — the old one may have expired, or been half-used.
          const fresh = target.current?.uploadUrl;
          if (fresh) {
            target.current!.uploadUrl = null;
            return fresh;
          }
          return (await api.reupload(projectId, file.name, file.size)).upload_url;
        },
        setProgress,
      );
      transfer.current = upload;
      await upload.promise;

      await api.uploadComplete(projectId);
      setNotice({ projectId, name: target.current?.name ?? file.name });
      aim(null);
      setFile(null);
      setName("");
      onCreated();
    } catch (err) {
      if (!cancelling.current) setError(errorMessage(err));
    } finally {
      transfer.current = null;
      setProgress(null);
    }
  }

  async function cancel() {
    cancelling.current = true;
    const projectId = target.current?.projectId;
    aim(null);
    await transfer.current?.cancel();
    // The project was made for this upload and holds nothing else: keeping
    // it would leave an empty project behind every cancelled upload.
    if (projectId) await api.deleteProject(projectId).catch(() => undefined);
    setProgress(null);
    setError(null);
    setCanceled(true);
    onCreated();
  }

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
          onChange={(e) => {
            pick(e.target.files?.[0] ?? null);
            // Cleared, or picking the same file again (the next video from
            // the same folder, a retry) fires no change at all.
            e.target.value = "";
          }}
        />

        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => inputRef.current?.click()} disabled={uploading}>
            Видео сонгох
          </Button>
          {file ? (
            <span className="min-w-0 text-sm text-ink-2 [overflow-wrap:anywhere]">
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
              disabled={uploading || resumable}
              maxLength={200}
            />
          </Field>
        )}

        {file && (
          <Field
            label="Видеонд ямар хэлээр ярьж байна"
            hint={
              language === "mn"
                ? undefined
                : "Яриаг тэр хэлээр нь танина. Санал гарсны дараа саналд орсон мөрүүдийг монгол руу өөрөө орчуулж, хадмал монголоор гарна."
            }
          >
            <Select
              value={language}
              onChange={(e) => setLanguage(e.target.value as SourceLanguage)}
              disabled={uploading || resumable}
              className="self-start"
            >
              {(Object.keys(LANGUAGE_LABELS) as SourceLanguage[]).map((code) => (
                <option key={code} value={code}>
                  {LANGUAGE_LABELS[code]}
                </option>
              ))}
            </Select>
          </Field>
        )}

        {uploading && file && (
          <div className="flex flex-col gap-2">
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span className="text-ink-2">Хуулж байна</span>
              <span className="tabular text-ink-3">
                {fileSize(Math.round((progress ?? 0) * file.size))} / {fileSize(file.size)} ·{" "}
                {Math.round((progress ?? 0) * 100)}%
              </span>
            </div>
            <ProgressBar value={progress ?? 0} />
            <p className="text-xs text-ink-3">
              Хуулж дуустал энэ табыг бүү хаа. Өөр хуудас руу шилжиж болно — хуулалт үргэлжилнэ.
            </p>
            <Button tone="quiet" className="self-start" onClick={() => void cancel()}>
              Цуцлах
            </Button>
          </div>
        )}

        {error && <Alert>{error}</Alert>}

        {canceled && !uploading && (
          <Alert tone="accent">Хуулалт цуцлагдлаа. Хоосон төсөл үлдээгээгүй.</Alert>
        )}

        {notice && !uploading && (
          <div className="flex flex-col gap-2">
            <Alert tone="accent">«{notice.name}» хуулагдлаа — видео боловсруулагдаж байна.</Alert>
            <Link
              href={`/projects/${notice.projectId}`}
              className={`${TAP} inline-flex items-center self-start text-sm font-medium text-accent underline`}
            >
              Төсөл рүү очих
            </Link>
          </div>
        )}

        {file && !uploading && (
          <Button tone="primary" onClick={() => void start()} className="self-start">
            {resumable ? "Үргэлжлүүлэх" : "Хуулж эхлэх"}
          </Button>
        )}
      </div>
    </Card>
  );
}
