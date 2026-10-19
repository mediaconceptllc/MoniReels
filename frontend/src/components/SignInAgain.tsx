"use client";

/**
 * Signing back in over the page, not instead of it.
 *
 * A session that ends mid-page — the token expired, or the password was
 * changed on another device — used to leave the producer on a page whose
 * every save failed with "Нэвтрэх хугацаа дууссан", and the only way back in
 * was the sign-in page: away from the transcript they had been correcting,
 * and from every unsaved line on it. This asks for the password where they
 * are; once it is accepted, the save they were trying works again.
 *
 * The account is fixed to the one that was signed in: the page belongs to it,
 * and signing in as someone else is the sign-in page's job.
 */

import { useRef, useState } from "react";
import { api } from "@/lib/api";
import { useDialogFocus } from "@/lib/dialog";
import type { TokenResponse } from "@/lib/types";
import { Alert, Button, Field, TextInput } from "@/components/ui";

export function SignInAgain({
  username,
  onSignedIn,
  onLeave,
}: {
  username: string;
  onSignedIn: (response: TokenResponse) => Promise<void>;
  onLeave: () => void;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLFormElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  // No Escape: this has to be answered — the page behind it cannot save.
  // Tab stays inside, so a keyboard user cannot wander onto the page's own
  // controls, all of which would fail with the same expired session.
  useDialogFocus(boxRef, { initial: passwordRef });

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSignedIn(await api.login(username, password));
    } catch (err) {
      // Not errorMessage() from lib/auth: that module renders this one.
      setError(err instanceof Error ? err.message : "Нэвтэрч чадсангүй.");
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Дахин нэвтрэх"
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-4"
    >
      <form
        ref={boxRef}
        onSubmit={submit}
        className="flex w-full max-w-md flex-col gap-4 rounded-lg border border-rule bg-surface p-5"
      >
        <p className="font-display text-base font-semibold text-ink">Нэвтрэх хугацаа дууссан</p>
        <p className="text-sm text-ink-2">
          Нууц үгээ дахин оруулбал энэ хуудаснаасаа үргэлжлүүлнэ — хадгалаагүй засвар алга
          болохгүй.
        </p>
        <p className="text-sm text-ink-2 [overflow-wrap:anywhere]">
          Бүртгэл: <span className="font-medium text-ink">{username}</span>
        </p>
        <Field label="Нууц үг">
          <TextInput
            ref={passwordRef}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" onClick={onLeave}>
            Өөр бүртгэлээр нэвтрэх
          </Button>
          <Button type="submit" tone="primary" loading={busy}>
            Нэвтрэх
          </Button>
        </div>
      </form>
    </div>
  );
}
