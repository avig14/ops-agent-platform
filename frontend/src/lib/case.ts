/** Pure helpers that read a CaseDetail. No React: easy to test. */
import type { Approval, CaseDetail, FieldRow, RawCaseDetail, Recommendation } from "../api/types";
import { humanize } from "./format";

/** The server sends `facts: null` while intake is still reading. Everything downstream can then rely on an object. */
export function normaliseDetail(raw: RawCaseDetail): CaseDetail {
  return { ...raw, facts: raw.facts ?? {} };
}

/** Highest round wins; the newest row breaks a tie. */
export function currentRecommendation(recs: readonly Recommendation[]): Recommendation | null {
  let best: Recommendation | null = null;
  for (const r of recs) {
    if (!best || r.round > best.round || (r.round === best.round && r.id > best.id)) best = r;
  }
  return best;
}

export function latestRound(approvals: readonly Approval[]): number | null {
  return approvals.length === 0 ? null : Math.max(...approvals.map((a) => a.round));
}

export interface RoundGroup {
  round: number;
  rows: Approval[];
}

/** Newest round first; inside a round, tier order. */
export function approvalsByRound(approvals: readonly Approval[]): RoundGroup[] {
  const map = new Map<number, Approval[]>();
  for (const a of approvals) map.set(a.round, [...(map.get(a.round) ?? []), a]);
  return [...map.entries()]
    .sort((x, y) => y[0] - x[0])
    .map(([round, rows]) => ({ round, rows: [...rows].sort((p, q) => p.tier - q.tier || p.id - q.id) }));
}

/** Approvals somebody can decide right now: open case, newest round, still pending. */
export function pendingDecisions(d: CaseDetail): Approval[] {
  if (d.case.status !== "AWAITING_APPROVAL" && d.case.status !== "ESCALATED") return [];
  const round = latestRound(d.approvals);
  if (round === null) return [];
  return d.approvals.filter((a) => a.round === round && a.status === "pending").sort((p, q) => p.tier - q.tier);
}

/** Who already approved a tier in this round (the same person may not approve twice). */
export function approvedBy(approvals: readonly Approval[], round: number): string[] {
  return approvals.filter((a) => a.round === round && a.status === "approved" && a.approver_id).map((a) => a.approver_id as string);
}

export interface Signal {
  label: string;
  value: "yes" | "no" | "n/a";
}

/** The three checks behind a confidence number, in plain words. */
export function signalList(s: FieldRow["signals"]): Signal[] {
  const v = (x: boolean | null | undefined): Signal["value"] => (x === true ? "yes" : x === false ? "no" : "n/a");
  return [
    { label: "Found in the message", value: v(s.grounded) },
    { label: "Rule check agrees", value: v(s.rule) },
    { label: "Second opinion agrees", value: v(s.second_opinion) },
  ];
}

/** One readable line from an agent payload: the event name, then the first few plain values. */
export function summarisePayload(payload: Record<string, unknown>): string {
  const ev = payload["event"];
  const parts: string[] = [];
  if (typeof ev === "string") parts.push(humanize(ev));
  for (const [k, v] of Object.entries(payload)) {
    if (k === "event" || parts.length >= 4) continue;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") parts.push(`${humanize(k)}: ${String(v)}`);
  }
  return parts.length > 0 ? parts.join(". ") : "Structured result, open the payload to read it.";
}

/** How many things on the Issues tab need a person. */
export function issueCount(d: CaseDetail): number {
  const f = d.facts;
  const conflicts = (f.conflicts ?? []).filter((c) => c.kind === "conflict" && !c.resolved).length;
  const pendingDup = d.related.possible_duplicates.filter((p) => !p.status || p.status === "pending").length;
  return (f.missing_detail?.length ?? f.missing?.length ?? 0) + conflicts + pendingDup;
}
