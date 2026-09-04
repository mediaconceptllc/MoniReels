"use client";

/** Shared primitives. Everything takes its colour from the tokens in
 *  globals.css, so both themes stay correct without per-component overrides. */

import { forwardRef } from "react";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

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
      className={`inline-flex items-center justify-center gap-2 rounded-md border px-3.5 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${TONE_CLASS[tone]} ${className}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
});

export function Spinner() {
  return (
    <span
      aria-hidden
      className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
    />
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
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[13px] font-medium text-ink-2">{label}</span>
      {children}
      {hint && <span className="text-xs text-ink-3">{hint}</span>}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`rounded-md border border-rule bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-3 ${props.className ?? ""}`}
    />
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
