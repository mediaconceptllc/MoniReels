"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth";
import { Button, TAP } from "@/components/ui";

export function Shell({ children }: { children: React.ReactNode }) {
  const { user, signOut } = useAuth();

  return (
    <div className="min-h-dvh">
      <header className="border-b border-rule bg-surface">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3.5 sm:px-6">
          <Link
            href="/"
            className={`${TAP} inline-flex shrink-0 items-center font-display text-lg font-semibold tracking-tight text-ink`}
          >
            MoniReels
          </Link>
          {user && (
            // min-w-0 lets the name give way. MEASURED at 360px with a
            // 38-character username: «Гарах» was pushed to x = 376–448,
            // off the screen, and the page scrolled sideways to reach it.
            <div className="flex min-w-0 items-center gap-3 text-sm">
              {/* Convenience only. /admin/* is closed on the server, so
                  hiding the link is not what keeps anyone out. */}
              {user.role === "admin" && (
                <Link
                  href="/admin"
                  className={`${TAP} inline-flex shrink-0 items-center px-1 text-ink-3 hover:text-ink`}
                >
                  Тохиргоо
                </Link>
              )}
              {/* The name was plain text, which left the password-change
                  endpoint with no way in at all. */}
              <Link
                href="/profile"
                title={user.username}
                className={`${TAP} inline-flex min-w-0 items-center px-1 text-ink-3 hover:text-ink`}
              >
                <span className="truncate">{user.username}</span>
              </Link>
              <Button tone="quiet" className="shrink-0" onClick={signOut}>
                Гарах
              </Button>
            </div>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">{children}</main>
    </div>
  );
}
