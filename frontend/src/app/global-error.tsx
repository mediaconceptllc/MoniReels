"use client";

/** The last resort: the root layout itself failed, so this draws its own
 *  document. Kept to plain markup — whatever broke the layout may be what
 *  the rest of the app depends on. */

import "./globals.css";

export default function GlobalError({ reset }: { error: Error; reset: () => void }) {
  return (
    <html lang="mn">
      <body>
        {/* Hoisted into <head> by React; a literal, like the rest of this
            page — nothing here may depend on the app that just failed. */}
        <title>Алдаа гарлаа · MoniReels</title>
        <main className="mx-auto flex min-h-dvh w-full max-w-xl flex-col justify-center gap-4 px-4 py-16">
          <h1 className="text-2xl font-semibold">Алдаа гарлаа</h1>
          <p className="text-sm text-ink-2">Програм ачаалах явцад алдаа гарлаа. Дахин оролдоно уу.</p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={reset}
              className="inline-flex min-h-11 items-center rounded-md border border-accent bg-accent px-4 text-sm font-medium text-paper"
            >
              Дахин оролдох
            </button>
            {/* A full page load on purpose: the router is part of what
                failed to render, so a client-side Link may not work. */}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
            <a
              href="/"
              className="inline-flex min-h-11 items-center rounded-md border border-rule bg-surface px-4 text-sm font-medium text-ink"
            >
              Төслүүд рүү буцах
            </a>
          </div>
        </main>
      </body>
    </html>
  );
}
