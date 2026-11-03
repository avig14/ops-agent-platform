import { describe, expect, it } from "vitest";
import { auditEvent } from "../test/factories";
import { AUDIT_FILTERS, matchesFilter, parseAuditFilter } from "./audit";

describe("parseAuditFilter", () => {
  it("accepts every known key", () => {
    for (const f of AUDIT_FILTERS) expect(parseAuditFilter(f.key)).toBe(f.key);
  });
  it("falls back to all for null, empty or unknown", () => {
    expect(parseAuditFilter(null)).toBe("all");
    expect(parseAuditFilter("")).toBe("all");
    expect(parseAuditFilter("<script>")).toBe("all");
  });
});

describe("matchesFilter", () => {
  const human = auditEvent({ actor_type: "human" });
  const agent = auditEvent({ actor_type: "agent" });
  const system = auditEvent({ actor_type: "system" });
  const refused = auditEvent({ actor_type: "human", outcome: "blocked" });
  const failed = auditEvent({ actor_type: "agent", outcome: "failure" });
  const degraded = auditEvent({ actor_type: "agent", outcome: "degraded" });
  const attempt = auditEvent({ outcome: "attempt" });

  it("all shows everything", () => {
    for (const e of [human, agent, system, refused, failed]) expect(matchesFilter(e, "all")).toBe(true);
  });
  it("filters by who acted", () => {
    expect(matchesFilter(human, "human")).toBe(true);
    expect(matchesFilter(agent, "human")).toBe(false);
    expect(matchesFilter(agent, "agent")).toBe(true);
    expect(matchesFilter(system, "system")).toBe(true);
    expect(matchesFilter(system, "agent")).toBe(false);
  });
  it("problems = failure, blocked (refusals) and degraded", () => {
    expect(matchesFilter(failed, "problems")).toBe(true);
    expect(matchesFilter(refused, "problems")).toBe(true);
    expect(matchesFilter(degraded, "problems")).toBe(true);
  });
  it("problems leaves out normal rows and in-flight attempts", () => {
    expect(matchesFilter(human, "problems")).toBe(false);
    expect(matchesFilter(attempt, "problems")).toBe(false);
  });
});
