/** Pure display helpers for the evaluation panel. No React, no state: easy to test. */
import type { EvalMode, EvalRate, EvalSnapshot } from "../api/types";

export const MODE_LABEL: Record<string, string> = {
  rules_only: "Rules only, no AI",
  llm: "AI, one model",
  "llm+second": "AI plus a second opinion",
};

const MODE_PREFERENCE = ["llm+second", "llm", "rules_only"];

export function pct(n: number | null | undefined): string {
  return n === null || n === undefined || Number.isNaN(n) ? "none" : `${Math.round(n * 100)}%`;
}

/** "56%" or "none" when nothing was measured. */
export function rateText(r: EvalRate | null | undefined): string {
  return r && r.rate !== null ? pct(r.rate) : "none";
}

/** "27 of 48" */
export function countText(r: EvalRate | null | undefined): string {
  return r ? `${r.k} of ${r.n}` : "none";
}

/** "likely between 44% and 68%". The interval is the honest part: small samples give wide ranges. */
export function rangeText(r: EvalRate | null | undefined): string {
  if (!r || r.rate === null) return "no data";
  return `between ${pct(r.ci95[0])} and ${pct(r.ci95[1])}`;
}

/** Share of a score between 0 and 1, as a number a CSS width can use. Clamped, never NaN. */
export function clamp01(n: number | null | undefined): number {
  return n === null || n === undefined || Number.isNaN(n) ? 0 : Math.min(1, Math.max(0, n));
}

/** The mode with real measurements to show, preferring the fullest pipeline. */
export function bestMode(modes: Record<string, EvalMode>): string | null {
  return MODE_PREFERENCE.find((m) => (modes[m]?.scored ?? 0) > 0) ?? null;
}

/** How many emails state this field (the denominator behind recall). A wrong value is counted once, inside `fn`. */
export function goldCount(row: { tp: number; fn: number }): number {
  return row.tp + row.fn;
}

export function staleText(because: string[]): string {
  const names = because.map((b) => (b === "prompt_hash" ? "the AI instructions" : b === "dataset_hash" ? "the test emails" : b));
  return `${names.join(" and ")} changed after this snapshot was taken. Re-run the evaluation before trusting these numbers.`;
}

export interface InjectionRow {
  label: string;
  attacks: number;
  detected: EvalRate;
  poison: EvalRate;
  utility: EvalRate;
}

export function injectionRows(s: EvalSnapshot["scripted"]["injection"]): InjectionRow[] {
  const row = (label: string, g: EvalSnapshot["scripted"]["injection"]["dev"]): InjectionRow => ({
    label,
    attacks: g.attacks,
    detected: g.detected,
    poison: g.attack_success_llm_path,
    utility: g.utility_under_attack,
  });
  return [row("Tuned-on attacks (dev)", s.dev), row("Unseen attacks (holdout)", s.holdout)];
}

/** Payloads the scanner missed at least once, worst first. */
export function weakPayloads(by: EvalSnapshot["scripted"]["injection"]["by_payload"]): { id: string; detected: EvalRate }[] {
  return Object.entries(by)
    .filter(([, g]) => (g.detected.rate ?? 1) < 1)
    .map(([id, g]) => ({ id, detected: g.detected }))
    .sort((a, b) => (a.detected.rate ?? 0) - (b.detected.rate ?? 0));
}
