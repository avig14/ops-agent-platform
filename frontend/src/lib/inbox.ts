import type { CaseRow } from "../api/types";
import { FINISHED, WORKING } from "./labels";

const NEEDS_PERSON = new Set(["NEEDS_INFO", "AWAITING_APPROVAL", "ESCALATED", "REJECTED"]);
const IN_MOTION = new Set(["APPROVED", "EXECUTING", "MONITORING"]);

interface View {
  key: string;
  label: string;
  test: (c: CaseRow) => boolean;
}

export const VIEWS: View[] = [
  { key: "all", label: "All", test: () => true },
  { key: "attention", label: "Needs a person", test: (c) => NEEDS_PERSON.has(c.status) },
  { key: "working", label: "AI working", test: (c) => WORKING.has(c.status) },
  { key: "motion", label: "In motion", test: (c) => IN_MOTION.has(c.status) },
  { key: "done", label: "Finished", test: (c) => FINISHED.has(c.status) },
];

/** Needs-a-person first, then priority (P1 first), then most recently touched. */
export function sortCases(rows: CaseRow[]): CaseRow[] {
  const pri = (c: CaseRow) => Number((c.priority ?? "P9").replace(/\D/g, "")) || 9;
  return [...rows].sort(
    (a, b) =>
      Number(NEEDS_PERSON.has(b.status)) - Number(NEEDS_PERSON.has(a.status)) ||
      pri(a) - pri(b) ||
      b.updated_at.localeCompare(a.updated_at),
  );
}

export function filterCases(rows: CaseRow[], view: string, q: string): CaseRow[] {
  const v = VIEWS.find((x) => x.key === view) ?? VIEWS[0]!;
  const needle = q.trim().toLowerCase();
  return rows.filter(
    (c) =>
      v.test(c) &&
      (!needle || c.id.toLowerCase().includes(needle) || (c.title ?? "").toLowerCase().includes(needle) || (c.region ?? "").toLowerCase().includes(needle)),
  );
}
