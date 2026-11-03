import { describe, expect, it } from "vitest";
import { approval, caseHead, detail, recommendation } from "../test/factories";
import { approvalsByRound, approvedBy, currentRecommendation, issueCount, latestRound, normaliseDetail, pendingDecisions, signalList, summarisePayload } from "./case";

describe("currentRecommendation", () => {
  it("is null with none", () => {
    expect(currentRecommendation([])).toBeNull();
  });
  it("takes the highest round", () => {
    const r = currentRecommendation([recommendation({ id: 5, round: 1 }), recommendation({ id: 2, round: 2 })]);
    expect(r?.id).toBe(2);
  });
  it("breaks a round tie with the newest id", () => {
    const r = currentRecommendation([recommendation({ id: 3, round: 1 }), recommendation({ id: 9, round: 1 }), recommendation({ id: 4, round: 1 })]);
    expect(r?.id).toBe(9);
  });
});

describe("rounds", () => {
  const a = [
    approval({ id: 1, round: 1, tier: 1 }),
    approval({ id: 4, round: 2, tier: 2 }),
    approval({ id: 3, round: 2, tier: 1 }),
    approval({ id: 2, round: 1, tier: 2 }),
  ];
  it("latestRound is the max, or null", () => {
    expect(latestRound(a)).toBe(2);
    expect(latestRound([])).toBeNull();
  });
  it("groups newest round first, tiers in order", () => {
    const g = approvalsByRound(a);
    expect(g.map((x) => x.round)).toEqual([2, 1]);
    expect(g[0]?.rows.map((r) => r.id)).toEqual([3, 4]);
    expect(g[1]?.rows.map((r) => r.id)).toEqual([1, 2]);
  });
  it("does not mutate the input", () => {
    const ids = a.map((x) => x.id);
    approvalsByRound(a);
    expect(a.map((x) => x.id)).toEqual(ids);
  });
});

describe("pendingDecisions", () => {
  const rows = [
    approval({ id: 1, round: 1, tier: 1, status: "rejected" }),
    approval({ id: 2, round: 2, tier: 2, status: "pending" }),
    approval({ id: 3, round: 2, tier: 1, status: "approved" }),
    approval({ id: 4, round: 2, tier: 1, status: "pending" }),
  ];
  it("lists only pending rows of the newest round, in tier order", () => {
    const d = detail({ case: caseHead({ status: "AWAITING_APPROVAL" }), approvals: rows });
    expect(pendingDecisions(d).map((x) => x.id)).toEqual([4, 2]);
  });
  it("includes escalated cases", () => {
    const d = detail({ case: caseHead({ status: "ESCALATED" }), approvals: rows });
    expect(pendingDecisions(d)).toHaveLength(2);
  });
  it("is empty once the case is not waiting on anyone", () => {
    for (const status of ["APPROVED", "CANCELLED", "CLOSED", "REJECTED", "NEEDS_INFO"] as const) {
      expect(pendingDecisions(detail({ case: caseHead({ status }), approvals: rows }))).toEqual([]);
    }
  });
  it("is empty with no approvals", () => {
    expect(pendingDecisions(detail({ case: caseHead({ status: "AWAITING_APPROVAL" }) }))).toEqual([]);
  });
});

describe("approvedBy", () => {
  it("lists approvers of that round only, skipping unsigned rows", () => {
    const rows = [
      approval({ round: 1, status: "approved", approver_id: "u-old" }),
      approval({ round: 2, status: "approved", approver_id: "u-a" }),
      approval({ round: 2, status: "rejected", approver_id: "u-b" }),
      approval({ round: 2, status: "approved", approver_id: null }),
      approval({ round: 2, status: "pending", approver_id: null }),
    ];
    expect(approvedBy(rows, 2)).toEqual(["u-a"]);
  });
});

describe("signalList", () => {
  it("maps true, false and missing to yes, no and n/a", () => {
    const s = signalList({ grounded: true, rule: false });
    expect(s).toEqual([
      { label: "Found in the message", value: "yes" },
      { label: "Rule check agrees", value: "no" },
      { label: "Second opinion agrees", value: "n/a" },
    ]);
  });
  it("treats null as n/a", () => {
    expect(signalList({ grounded: null, rule: null, second_opinion: null }).every((x) => x.value === "n/a")).toBe(true);
  });
});

describe("summarisePayload", () => {
  it("leads with the event, then plain values", () => {
    expect(summarisePayload({ event: "intake_done", fields: 5, degraded: false })).toBe("Intake done. Fields: 5. Degraded: false");
  });
  it("skips nested values", () => {
    expect(summarisePayload({ event: "x", nested: { a: 1 }, list: [1] })).toBe("X");
  });
  it("caps at four parts", () => {
    const out = summarisePayload({ event: "e", a: 1, b: 2, c: 3, d: 4, f: 5 });
    expect(out.split(". ")).toHaveLength(4);
  });
  it("has a fallback when nothing is readable", () => {
    expect(summarisePayload({ nested: { a: 1 } })).toMatch(/open the payload/i);
    expect(summarisePayload({})).toMatch(/open the payload/i);
  });
});

describe("issueCount", () => {
  const dup = { id: "CASE-9", kind: "possible", score: 0.8 };
  it("counts missing fields, open conflicts and pending duplicates", () => {
    const d = detail({
      facts: {
        missing_detail: [{ field: "supplier", why: "not stated" }],
        conflicts: [
          { field: "qty", email: 500, system: 120, kind: "conflict" },
          { field: "qty2", email: 1, system: 2, kind: "conflict", resolved: "system" },
          { field: "stock", email: 1, system: 2, kind: "stale_snapshot" },
        ],
      },
      related: { duplicate_of: null, parent: null, children: [], possible_duplicates: [dup, { ...dup, id: "CASE-8", status: "dismissed" }] },
    });
    expect(issueCount(d)).toBe(1 + 1 + 1);
  });
  it("falls back to the plain missing list", () => {
    expect(issueCount(detail({ facts: { missing: ["a", "b"] } }))).toBe(2);
  });
  it("is zero for a clean case", () => {
    expect(issueCount(detail())).toBe(0);
  });
});

describe("normaliseDetail", () => {
  it("turns the null facts the server sends before intake has run into an empty object", () => {
    const raw = { ...detail(), facts: null };
    const d = normaliseDetail(raw);
    expect(d.facts).toEqual({});
    expect(issueCount(d)).toBe(0);
  });
  it("keeps real facts untouched", () => {
    const d = normaliseDetail({ ...detail(), facts: { item: "shelf" } });
    expect(d.facts).toEqual({ item: "shelf" });
  });
});
