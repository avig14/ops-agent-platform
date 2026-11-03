/** Pure display helpers. No React, no state: easy to test. */

const dateTime = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hour12: true });
const dateOnly = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric" });
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const plain = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 });

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtDateTime(iso: string | null | undefined): string {
  const d = parse(iso);
  return d ? dateTime.format(d) : "none";
}

export function fmtDate(iso: string | null | undefined): string {
  const d = parse(iso);
  return d ? dateOnly.format(d) : "none";
}

export function fmtInr(n: number | string | null | undefined): string {
  const v = typeof n === "string" ? Number(n) : n;
  if (v === null || v === undefined || Number.isNaN(v)) return "none";
  return `INR ${inr.format(v)}`;
}

export function fmtNumber(n: number | null | undefined): string {
  return n === null || n === undefined || Number.isNaN(n) ? "none" : plain.format(n);
}

export function fmtPct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

export function fmtHours(h: number | null | undefined): string {
  if (h === null || h === undefined) return "none yet";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${plain.format(h)} h`;
  return `${plain.format(h / 24)} d`;
}

/** "in 3 h 20 min", "overdue by 2 h". `now` is injectable so tests stay deterministic. */
export function timeLeft(iso: string | null | undefined, now: number = Date.now()): { text: string; overdue: boolean } | null {
  const d = parse(iso);
  if (!d) return null;
  const diffMin = Math.round((d.getTime() - now) / 60000);
  if (diffMin === 0) return { text: "due now", overdue: false };
  const span = spanText(Math.abs(diffMin));
  return diffMin < 0 ? { text: `overdue by ${span}`, overdue: true } : { text: `in ${span}`, overdue: false };
}

/** A sentence for a deadline: "Due in 3 h", "Overdue by 2 h", "Due now". `label` is the word for the "in ..." form. */
export function dueLabel(t: { text: string; overdue: boolean }, label = "Due"): string {
  if (t.overdue || t.text === "due now") return t.text.charAt(0).toUpperCase() + t.text.slice(1);
  return `${label} ${t.text}`;
}

/** Two units at most, and never a "0 min" tail: 200 -> "3 h 20 min", 180 -> "3 h", 1500 -> "1 d 1 h". */
function spanText(totalMin: number): string {
  const days = Math.floor(totalMin / 1440);
  const hours = Math.floor((totalMin % 1440) / 60);
  const mins = totalMin % 60;
  if (days > 0) return hours > 0 ? `${days} d ${hours} h` : `${days} d`;
  if (hours > 0) return mins > 0 ? `${hours} h ${mins} min` : `${hours} h`;
  return `${mins} min`;
}

export function ageText(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

/** "expedite_alternate_supplier" -> "Expedite alternate supplier" */
export function humanize(s: string | null | undefined): string {
  if (!s) return "none";
  const t = s.replace(/[_-]+/g, " ").trim().toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export function shortHash(h: string | null | undefined, n = 10): string {
  return h ? h.slice(0, n) : "none";
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
