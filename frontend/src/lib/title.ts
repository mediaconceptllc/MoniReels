"use client";

/**
 * The browser tab's title, per page.
 *
 * Every page was titled "MoniReels" (audit A11Y-3): five tabs of the same
 * product read the same in the tab strip, in history and to a screen reader
 * announcing the page — "which project was that?" had no answer short of
 * clicking through them. The pages are client components, so the static
 * `metadata` export is not theirs to use; this sets it once the page knows
 * what it is showing (a project's name arrives with its data).
 *
 * `npm run check-ui` holds every page to calling this.
 */

import { useEffect } from "react";
import { pageTitle } from "./page-title";

export function usePageTitle(title: string | null | undefined) {
  useEffect(() => {
    document.title = pageTitle(title);
  }, [title]);
}
