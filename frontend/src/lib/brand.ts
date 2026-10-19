"use client";

/**
 * Which brand files exist, for the panel that offers to use them.
 *
 * The logo, intro and outro were offered whether or not anything had been
 * uploaded, and the worker skips a missing one with a log line nobody reads
 * — so "Лого тавих" could be ticked, saved, exported and silently absent
 * (audit UX-8). The answer is the readiness endpoint's: whether each file
 * exists, never where it is.
 */

import { useEffect, useState } from "react";
import { api } from "./api";
import type { BrandAvailability } from "./types";

/** Null until known, and on a failed read: a choice is never disabled on a
 *  guess — the panel behaves as it did before it could ask. */
export function useBrandAvailability(): BrandAvailability | null {
  const [brand, setBrand] = useState<BrandAvailability | null>(null);
  useEffect(() => {
    let cancelled = false;
    void api
      .providerReadiness()
      .then((r) => {
        if (!cancelled) setBrand(r.brand ?? null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return brand;
}

/** Why a brand choice cannot be turned on, in the reader's terms — or null
 *  when it can. An editor is told who can fix it, not sent to a page that
 *  turns them away. */
export function brandMissing(
  available: boolean | undefined,
  what: string,
  isAdmin: boolean,
): string | null {
  if (available !== false) return null;
  return isAdmin
    ? `${what} оруулаагүй байна — Тохиргоо → Брэндийн материалаас оруулна.`
    : `${what} оруулаагүй байна — админаас оруулахыг хүснэ үү.`;
}
