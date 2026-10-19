"use client";

/**
 * Keyboard focus for a modal dialog: in on open, held inside while open, and
 * back where it came from on close.
 *
 * MEASURED before this existed (audit A11Y-2, Chromium): from the delete
 * confirmation one Tab already left the dialog for the page behind it, and
 * Escape dropped focus on the first rail button — not on «Төсөл устгах», the
 * button that had opened it. The cut preview never took focus at all: a
 * keyboard user opened it and was still on the card underneath. Three
 * dialogs each wrote their own Escape handler and none did the rest, so the
 * rest lives here, once — `npm run check-ui` refuses a `role="dialog"` that
 * does not call this.
 *
 * Dialogs stack: a session that expires while a confirmation is open puts
 * the sign-in dialog on top of it, and only the top one may own the keys.
 */

import { useEffect, useRef } from "react";
import type { RefObject } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  'input:not([disabled]):not([type="hidden"])',
  "select:not([disabled])",
  "textarea:not([disabled])",
  "video[controls]",
  "audio[controls]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

const stack: symbol[] = [];

function focusables(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (el) => el.tabIndex >= 0 && el.getClientRects().length > 0,
  );
}

export function useDialogFocus(
  box: RefObject<HTMLElement | null>,
  {
    onClose,
    initial,
  }: {
    /** Escape. Absent for a dialog that must be answered (signing in again). */
    onClose?: () => void;
    /** Where focus lands. Default: the first control inside. A destructive
     *  confirmation passes its way OUT — a stray Enter must not delete. */
    initial?: RefObject<HTMLElement | null>;
  } = {},
) {
  const id = useRef(Symbol("dialog"));
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const me = id.current;
    // Whoever had focus is who opened this; that is where it goes back.
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    stack.push(me);
    const root = box.current;
    const target = initial?.current ?? (root ? focusables(root)[0] : null) ?? root;
    target?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (stack[stack.length - 1] !== me) return;
      if (e.key === "Escape" && close.current) {
        e.preventDefault();
        close.current();
        return;
      }
      if (e.key !== "Tab" || !root) return;
      const items = focusables(root);
      if (items.length === 0) {
        e.preventDefault();
        root.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (!root.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);

    return () => {
      document.removeEventListener("keydown", onKey);
      const at = stack.lastIndexOf(me);
      if (at >= 0) stack.splice(at, 1);
      // Only if it is still on the page: a delete that succeeded navigates
      // away, and focusing a detached button would put focus nowhere.
      if (opener?.isConnected) opener.focus();
    };
    // Mount and unmount only: the dialog's own re-renders must not pull
    // focus back to its first control while someone is typing in it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
