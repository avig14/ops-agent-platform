import { describe, expect, it } from "vitest";
import { caseRow } from "../test/factories";
import { filterCases, sortCases, VIEWS } from "./inbox";

const rows = [
  caseRow({ id: "CASE-1", status: "CLOSED", priority: "P1", title: "Old closed", region: "West" }),
  caseRow({ id: "CASE-2", status: "EXTRACTING", priority: "P2", title: "Reading now", region: "South" }),
  caseRow({ id: "CASE-3", status: "AWAITING_APPROVAL", priority: "P3", title: "Waiting for head", region: "West" }),
  caseRow({ id: "CASE-4", status: "EXECUTING", priority: "P2", title: "Plan running", region: "West" }),
  caseRow({ id: "CASE-5", status: "NEEDS_INFO", priority: "P1", title: "Missing supplier", region: "South" }),
];

describe("filterCases", () => {
  it("all keeps everything", () => {
    expect(filterCases(rows, "all", "")).toHaveLength(5);
  });
  it("attention = states that wait on a person", () => {
    expect(filterCases(rows, "attention", "").map((c) => c.id).sort()).toEqual(["CASE-3", "CASE-5"]);
  });
  it("working = AI mid-pipeline", () => {
    expect(filterCases(rows, "working", "").map((c) => c.id)).toEqual(["CASE-2"]);
  });
  it("motion = approved and carrying on", () => {
    expect(filterCases(rows, "motion", "").map((c) => c.id)).toEqual(["CASE-4"]);
  });
  it("done = finished states", () => {
    expect(filterCases(rows, "done", "").map((c) => c.id)).toEqual(["CASE-1"]);
  });
  it("an unknown view falls back to all", () => {
    expect(filterCases(rows, "nonsense", "")).toHaveLength(5);
  });
  it("search matches id, title and region, ignoring case and padding", () => {
    expect(filterCases(rows, "all", "  case-3 ").map((c) => c.id)).toEqual(["CASE-3"]);
    expect(filterCases(rows, "all", "MISSING").map((c) => c.id)).toEqual(["CASE-5"]);
    expect(filterCases(rows, "all", "south").map((c) => c.id).sort()).toEqual(["CASE-2", "CASE-5"]);
  });
  it("view and search combine", () => {
    expect(filterCases(rows, "attention", "south").map((c) => c.id)).toEqual(["CASE-5"]);
  });
  it("copes with a null title and region", () => {
    const r = [caseRow({ id: "X", title: null, region: null })];
    expect(filterCases(r, "all", "zzz")).toEqual([]);
    expect(filterCases(r, "all", "x")).toHaveLength(1);
  });
  it("every view key is unique", () => {
    expect(new Set(VIEWS.map((v) => v.key)).size).toBe(VIEWS.length);
  });
});

describe("sortCases", () => {
  it("puts cases that need a person first, then P1 before P3", () => {
    // Needs a person: CASE-5 (P1) then CASE-3 (P3). The rest: CASE-1 (P1), then the P2 pair in input order.
    expect(sortCases(rows).map((c) => c.id)).toEqual(["CASE-5", "CASE-3", "CASE-1", "CASE-2", "CASE-4"]);
  });
  it("breaks priority ties by newest update", () => {
    const a = caseRow({ id: "A", status: "EXECUTING", priority: "P2", updated_at: "2026-11-03T08:00:00Z" });
    const b = caseRow({ id: "B", status: "EXECUTING", priority: "P2", updated_at: "2026-11-03T09:00:00Z" });
    expect(sortCases([a, b]).map((c) => c.id)).toEqual(["B", "A"]);
  });
  it("a missing priority sorts last", () => {
    const a = caseRow({ id: "A", status: "EXECUTING", priority: null });
    const b = caseRow({ id: "B", status: "EXECUTING", priority: "P4" });
    expect(sortCases([a, b]).map((c) => c.id)).toEqual(["B", "A"]);
  });
  it("does not mutate its input", () => {
    const before = rows.map((c) => c.id);
    sortCases(rows);
    expect(rows.map((c) => c.id)).toEqual(before);
  });
});
