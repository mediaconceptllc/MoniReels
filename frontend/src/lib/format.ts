/**
 * Every conversion from a number to something a person reads happens here.
 *
 * Timings arrive as float seconds and stay that way in state; formatting is a
 * render-time concern. The desktop build learned the same lesson in reverse —
 * a formatted string that leaks into state is one nobody can do arithmetic on.
 */

import type { SourceLanguage } from "@/lib/types";

export function timecode(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return h > 0 ? `${h}:${mm}:${String(s).padStart(2, "0")}` : `${mm}:${String(s).padStart(2, "0")}`;
}

export function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)} сек`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  return `${hours} ц ${minutes % 60} мин`;
}

export function fileSize(bytes: number): string {
  if (!bytes) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** Dollars, at the precision the number actually has.
 *
 *  Two decimal places would print "$0.00" for a real charge: a suggestion run
 *  on a cheap model lands at a fraction of a cent, and the backend keeps six
 *  places for exactly that reason. Rounding a bill to zero is the one error
 *  that reads as good news. */
export function usd(amount: number): string {
  if (!Number.isFinite(amount) || amount < 0) return "—";
  if (amount === 0) return "$0";
  if (amount >= 1) return `$${amount.toFixed(2)}`;
  if (amount >= 0.01) return `$${amount.toFixed(3)}`;
  if (amount >= 0.0001) return `$${amount.toFixed(4)}`;
  return "<$0.0001";
}

/** "5 минутын өмнө", written out by hand.
 *
 *  It was `Intl.RelativeTimeFormat("mn")`, which Chromium does not ship
 *  Mongolian data for: it falls back to English, so every card, job row and
 *  profile line read "-1 h", "-8 d" or "yesterday" in an interface that is
 *  otherwise entirely Mongolian. Mongolian needs no plural rules here — the
 *  genitive before өмнө / дараа is the same for every number. */
export function relativeTime(epochSeconds: number, now: number = Date.now()): string {
  const diff = epochSeconds * 1000 - now;
  const past = diff <= 0;
  const minutes = Math.round(Math.abs(diff) / 60000);
  const say = (n: number, unit: string) => `${n} ${unit} ${past ? "өмнө" : "дараа"}`;
  if (minutes < 1) return past ? "дөнгөж сая" : "удахгүй";
  if (minutes < 60) return say(minutes, "минутын");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return say(hours, "цагийн");
  const days = Math.round(hours / 24);
  if (days === 1) return past ? "өчигдөр" : "маргааш";
  if (days < 30) return say(days, "өдрийн");
  const date = new Date(epochSeconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}.${pad(date.getMonth() + 1)}.${pad(date.getDate())}`;
}

/** What a video can be spoken in, as the producer reads it. Typed by the
 *  union so a language added to it without a name here does not compile. */
export const LANGUAGE_LABELS: Record<SourceLanguage, string> = {
  mn: "Монгол",
  en: "Англи",
};

/** Roles are English in the data (they are structural), Mongolian on screen. */
export const CUT_ROLE_LABELS: Record<string, string> = {
  hook: "Дэгээ",
  context: "Дэвсгэр",
  proof: "Нотолгоо",
  payoff: "Оргил",
};

export const OUTPUT_KIND_LABELS: Record<string, string> = {
  reel: "Богино видео",
  youtube: "YouTube хураангуй",
  export: "Гараар огтолсон",
};

export function totalCutSeconds(cuts: { start: number; end: number }[]): number {
  return cuts.reduce((sum, cut) => sum + Math.max(0, cut.end - cut.start), 0);
}
