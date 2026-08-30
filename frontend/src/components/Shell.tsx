"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui";

export function Shell({ children }: { children: React.ReactNode }) {
  const { user, signOut } = useAuth();

  return (
    <div className="min-h-dvh">
      <header className="border-b border-rule bg-surface">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3.5">
          <Link href="/" className="font-display text-lg font-semibold tracking-tight text-ink">
            MoniReels
          </Link>
          {user && (
            <div className="flex items-center gap-3 text-sm">
              {/* Convenience only. /admin/* is closed on the server, so
                  hiding the link is not what keeps anyone out. */}
              {user.role === "admin" && (
                <Link href="/admin" className="text-ink-3 hover:text-ink">
                  Тохиргоо
                </Link>
              )}
              <span className="text-ink-3">{user.username}</span>
              <Button tone="quiet" onClick={signOut}>
                Гарах
              </Button>
            </div>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
    </div>
  );
}
