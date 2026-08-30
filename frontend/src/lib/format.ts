/**
 * Every conversion from a number to something a person reads happens here.
 *
 * Timings arrive as float seconds and stay that way in state; formatting is a
 * render-time concern. The desktop build learned the same lesson in reverse —
 * a formatted string that leaks into state is one nobody can do arithmetic on.
 */

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

const RELATIVE = new Intl.RelativeTimeFormat("mn", { numeric: "auto" });

export function relativeTime(epochSeconds: number): string {
  const diff = epochSeconds * 1000 - Date.now();
  const minutes = Math.round(diff / 60000);
  if (Math.abs(minutes) < 60) return RELATIVE.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return RELATIVE.format(hours, "hour");
  return RELATIVE.format(Math.round(hours / 24), "day");
}

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
