"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Capability } from "@/lib/types";
import { Alert } from "@/components/ui";

/**
 * What cannot run right now, said before the button is pressed.
 *
 * The API refuses a job it cannot finish, so nothing here is a security
 * control — it is the difference between finding out now and finding out
 * after a worker slot, a download and a failed job. Production hit exactly
 * that: an empty duudlaga.dev balance, 62 rejected chunks, and the operator's
 * first notice was a stack trace.
 *
 * TTS is left out unless something asks for it: nothing in the pipeline uses
 * it yet, so warning about it on every project page is noise that trains
 * people to ignore the banner.
 */
export function ProviderWarnings({ only }: { only?: Capability["name"][] }) {
  const [blocked, setBlocked] = useState<Capability[]>([]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const { capabilities } = await api.providerReadiness();
        if (cancelled) return;
        setBlocked(
          capabilities.filter(
            (c) => !c.ready && c.blocked && (only ? only.includes(c.name) : c.name !== "tts"),
          ),
        );
      } catch {
        // A readiness check that cannot itself be reached is not worth a
        // banner of its own — whatever the operator does next will say so.
      }
    })();
    return () => {
      cancelled = true;
    };
    // `only` is a literal at every call site; re-running on identity would
    // refetch on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (blocked.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      {blocked.map((c) => (
        <Alert key={c.name}>
          <span className="font-medium">{c.label}:</span> {c.blocked}
        </Alert>
      ))}
    </div>
  );
}
