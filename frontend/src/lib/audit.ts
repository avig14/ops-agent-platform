import type { AuditEvent } from "../api/types";

export type AuditFilter = "all" | "human" | "agent" | "system" | "problems";

export const AUDIT_FILTERS: { key: AuditFilter; label: string }[] = [
  { key: "all", label: "Everything" },
  { key: "human", label: "People" },
  { key: "agent", label: "AI" },
  { key: "system", label: "System" },
  { key: "problems", label: "Failures and refusals" },
];

export function parseAuditFilter(raw: string | null): AuditFilter {
  return AUDIT_FILTERS.find((f) => f.key === raw)?.key ?? "all";
}

export function matchesFilter(e: AuditEvent, f: AuditFilter): boolean {
  if (f === "all") return true;
  if (f === "problems") return e.outcome === "failure" || e.outcome === "blocked" || e.outcome === "degraded";
  return e.actor_type === f;
}
