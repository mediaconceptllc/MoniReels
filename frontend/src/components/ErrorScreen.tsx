/**
 * The page shown when there is no page — an unknown address, or a screen that
 * failed while drawing.
 *
 * Without one, Next.js shows its own: "404 | This page could not be found."
 * and "Application error: a client-side exception has occurred", in English,
 * with no way back. A response the page did not expect (one missing field)
 * turned the whole app into that second screen.
 */

import Link from "next/link";
import type { ReactNode } from "react";

export function ErrorScreen({
  title,
  hint,
  children,
}: {
  title: string;
  hint: string;
  /** Anything to offer besides the way home — a retry, for instance. */
  children?: ReactNode;
}) {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-xl flex-col justify-center gap-4 px-4 py-16">
      <h1 className="font-display text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="text-sm text-ink-2">{hint}</p>
      <div className="flex flex-wrap gap-2">
        {children}
        <Link
          href="/"
          className="inline-flex min-h-11 items-center rounded-md border border-rule bg-surface px-4 text-sm font-medium text-ink hover:bg-surface-2"
        >
          Төслүүд рүү буцах
        </Link>
      </div>
    </main>
  );
}
