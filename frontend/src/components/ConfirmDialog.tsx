"use client";

/**
 * Confirming something that cannot be undone.
 *
 * `window.confirm` was doing this job in two places, and it is wrong here for
 * three reasons that all matter to this product: its buttons are the
 * browser's, so they read OK/Cancel in whatever language the browser is set
 * to — English, in a product whose every other string is Mongolian; it can
 * carry one line of text and no structure; and it says nothing about what is
 * actually about to be lost.
 *
 * Deleting a project takes its transcript, its suggestions and every video
 * rendered from it. Those are hours of work and real money spent, and the
 * producer deserves to see them counted before the click, not discover them
 * afterwards.
 */

import { useEffect, useRef } from "react";
import { Button } from "@/components/ui";

export function ConfirmDialog({
  title,
  lose,
  confirmLabel,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  /** What goes, counted. Empty is fine — some things really do stand alone. */
  lose?: string[];
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Focus lands on the way OUT, never on the destructive button: a stray
  // Enter must not delete a project.
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-4"
      onClick={onCancel}
    >
      <div
        className="flex w-full max-w-md flex-col gap-4 rounded-lg border border-rule bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="font-display text-base font-semibold text-ink">{title}</p>

        {lose && lose.length > 0 && (
          <div className="rounded-md border border-tally/40 bg-tally-soft px-3.5 py-3">
            <p className="text-[13px] font-medium text-tally">Хамт устах зүйлс:</p>
            <ul className="mt-1.5 flex flex-col gap-0.5">
              {lose.map((line) => (
                <li key={line} className="tabular text-[13px] text-tally">
                  · {line}
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="text-sm text-ink-3">Энэ үйлдлийг буцаах боломжгүй.</p>

        <div className="flex flex-wrap justify-end gap-2">
          <Button ref={cancelRef} className="min-h-[44px] px-4" onClick={onCancel}>
            Болих
          </Button>
          <Button tone="danger" className="min-h-[44px] px-4" loading={busy} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
