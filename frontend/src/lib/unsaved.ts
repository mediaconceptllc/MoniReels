"use client";

/**
 * Leaving a page that holds unsaved work, asked first.
 *
 * The project page keeps edits in memory until "Хадгалах" — a transcript
 * corrected line by line, settings chosen but not yet sent. Two ways out used
 * to drop them without a word: closing or reloading the tab, and any link in
 * the header. Both now stop and ask.
 *
 * The tab is the browser's own prompt (`beforeunload`): its wording is the
 * browser's and cannot be changed, but it is the only thing that can stop a
 * closing tab. A link is caught before Next.js sees the click, in the capture
 * phase on the document, so the page can ask in its own dialog and then
 * navigate itself.
 *
 * NOT covered: the browser's Back button inside the app. App Router moves
 * between pages with the history API and offers no event to cancel it.
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

/** The in-app address a click on `anchor` would go to, or null when the click
 *  is not one this guard should hold: a new tab, a download, another site, a
 *  modified click, or the page that is already open. */
function heldTarget(event: MouseEvent): string | null {
  if (event.defaultPrevented || event.button !== 0) return null;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return null;
  const anchor = (event.target as Element | null)?.closest?.("a[href]");
  if (!(anchor instanceof HTMLAnchorElement)) return null;
  if ((anchor.target && anchor.target !== "_self") || anchor.hasAttribute("download")) return null;
  const url = new URL(anchor.href, window.location.href);
  if (url.origin !== window.location.origin) return null;
  if (url.pathname === window.location.pathname && url.search === window.location.search) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

export function useLeaveGuard(dirty: boolean) {
  const router = useRouter();
  const [leaveTo, setLeaveTo] = useState<string | null>(null);
  // Read by the listeners, which are added once: re-adding them on every
  // keystroke's change of count would be churn for nothing.
  const dirtyRef = useRef(dirty);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    const onUnload = (event: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return;
      event.preventDefault();
      // Chrome before 119 still needs the legacy property set.
      event.returnValue = "";
    };
    const onClick = (event: MouseEvent) => {
      if (!dirtyRef.current) return;
      const target = heldTarget(event);
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      setLeaveTo(target);
    };
    window.addEventListener("beforeunload", onUnload);
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener("beforeunload", onUnload);
      document.removeEventListener("click", onClick, true);
    };
  }, []);

  return {
    /** Where a held click was going, while the page asks. */
    leaveTo,
    stay: () => setLeaveTo(null),
    leave: () => {
      const target = leaveTo;
      setLeaveTo(null);
      // Cleared first: the guard must not catch its own navigation.
      dirtyRef.current = false;
      if (target) router.push(target);
    },
  };
}
