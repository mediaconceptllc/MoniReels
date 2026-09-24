"use client";

/** Shared primitives. Everything takes its colour from the tokens in
 *  globals.css, so both themes stay correct without per-component overrides. */

import { forwardRef } from "react";
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from "react";

/**
 * The floor every tap target stands on: 44 CSS px.
 *
 * It lives here, in the primitives, rather than at each call site — a floor
 * repeated at thirty call sites is a floor the thirty-first forgets, and the
 * thirty-first is always the newest control. The default button was
 * `py-2 text-sm`: 8 + 20 + 8 + 2 = 38px, under every published minimum (Apple
 * asks 44, Material 48) and small enough that a thumb aiming at "Устгах" in a
 * card footer lands on the card behind it.
 *
 * It cannot be lowered by appending a smaller class at a call site: two
 * Tailwind rules of equal specificity are decided by their order in the
 * STYLESHEET, not in the class attribute, and Tailwind emits height utilities
 * in ascending order (MEASURED in the built CSS: h-3, h-4, h-5, h-8, h-11,
 * h-20 …). So a smaller one loses and a larger one wins — which is the right
 * way round, since a taller target was never the problem. The floor moves
 * only by editing this line, and `npm run check-ui` fails if it moves down.
 */
export const TAP = "min-h-11";

type Tone = "default" | "primary" | "danger" | "quiet";

const TONE_CLASS: Record<Tone, string> = {
  default: "bg-surface text-ink border-rule hover:bg-surface-2",
  primary: "bg-accent text-paper border-accent hover:opacity-90",
  // Red is reserved for something genuinely destructive — never decoration.
  danger: "bg-tally-soft text-tally border-tally/40 hover:bg-tally/15",
  quiet: "bg-transparent text-ink-2 border-transparent hover:bg-surface-2",
};

/** Forwards its ref so a dialog can put focus on the way OUT rather than on
 *  the destructive button — a stray Enter must not delete anything. */
export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone; loading?: boolean }
>(function Button({ tone = "default", loading = false, children, className = "", disabled, ...rest }, ref) {
  return (
    <button
      {...rest}
      ref={ref}
      disabled={disabled || loading}
      className={`${TAP} inline-flex items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${TONE_CLASS[tone]} ${className}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
});

/** Belongs to a CONTROL that is working — the spinner inside a button that was
 *  just pressed. It is never a page's loading state: a spinner standing in for
 *  a layout takes the layout away with it, and the reader has to find their
 *  place again when it comes back. That job is `Skeleton`'s, and
 *  `npm run check-ui` keeps this component inside this file. */
export function Spinner() {
  return (
    <span
      aria-hidden
      className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
    />
  );
}

/** A block standing where content has not arrived yet, in its shape.
 *
 *  Decorative by construction — `Loading` carries the announcement, so the
 *  blocks themselves are hidden from a screen reader rather than read out as a
 *  row of empty boxes. The pulse is an `animation`, so the
 *  prefers-reduced-motion rule in globals.css already stills it. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <span aria-hidden className={`block animate-pulse rounded bg-surface-2 ${className}`} />;
}

/** Wraps a region of skeletons so the wait is announced once, with a name,
 *  instead of silently or as a heap of blank elements. */
export function Loading({
  label = "Ачаалж байна",
  className = "",
  children,
}: {
  label?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div role="status" aria-busy="true" aria-label={label} className={className}>
      {children}
    </div>
  );
}

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-lg border border-rule bg-surface ${className}`}>{children}</div>
  );
}

export function Badge({
  tone = "default",
  children,
}: {
  tone?: "default" | "fit" | "warn" | "danger" | "accent";
  children: ReactNode;
}) {
  const map = {
    default: "bg-surface-2 text-ink-2",
    fit: "bg-fit-soft text-fit",
    warn: "bg-warn-soft text-warn",
    danger: "bg-tally-soft text-tally",
    accent: "bg-accent-soft text-accent",
  } as const;
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium tracking-wide ${map[tone]}`}
    >
      {children}
    </span>
  );
}

export function ProgressBar({ value, tone = "accent" }: { value: number; tone?: "accent" | "fit" }) {
  const percent = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
      role="progressbar"
      aria-valuenow={percent}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={`h-full rounded-full transition-[width] duration-300 ${tone === "fit" ? "bg-fit" : "bg-accent"}`}
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}

export function Field({
  label,
  hint,
  aside,
  children,
}: {
  label: string;
  hint?: string;
  /** Rendered beside the label — a live status, a count. It stays INSIDE the
   *  label element: hand-rolling the row outside it is how a field loses its
   *  `<label>`, and with it the click that focuses the control. */
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium text-ink-2">{label}</span>
        {aside}
      </span>
      {children}
      {hint && <span className="text-xs text-ink-3">{hint}</span>}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`${TAP} rounded-md border border-rule bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-3 ${props.className ?? ""}`}
    />
  );
}

/** The same class string lived inline in one panel and as a `const SELECT` in
 *  another — which is how one of them gets a floor and the other does not. */
export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={`${TAP} rounded-md border border-rule bg-surface px-3 py-2 text-sm text-ink ${props.className ?? ""}`}
    />
  );
}

/**
 * A checkbox and the words that explain it, as one target.
 *
 * A bare checkbox renders at about 13px — the smallest thing on any page it
 * appears on, and the one most often tapped by someone holding a phone in one
 * hand. The box is drawn bigger, but the TARGET is the label: it carries the
 * floor, so the whole row toggles, which is also what a pointer user expects.
 */
export function Checkbox({
  checked,
  onChange,
  disabled,
  children,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <label
      className={`${TAP} flex items-center gap-2.5 text-sm text-ink-2 ${
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"
      }`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="h-5 w-5 shrink-0 accent-[var(--accent)]"
      />
      {children}
    </label>
  );
}

export function Alert({
  tone = "danger",
  children,
}: {
  tone?: "danger" | "warn" | "accent";
  children: ReactNode;
}) {
  const map = {
    danger: "border-tally/40 bg-tally-soft text-tally",
    warn: "border-warn/40 bg-warn-soft text-warn",
    accent: "border-accent/30 bg-accent-soft text-accent",
  } as const;
  return (
    <div role="alert" className={`rounded-md border px-3.5 py-2.5 text-sm ${map[tone]}`}>
      {children}
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-rule px-6 py-12 text-center">
      <p className="font-display text-base font-medium text-ink">{title}</p>
      {hint && <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-3">{hint}</p>}
    </div>
  );
}
