import { describe, expect, it } from "vitest";
import { diffLines } from "./diff";

describe("diffLines", () => {
  it("has nothing to show for null or undefined", () => {
    expect(diffLines(null)).toEqual([]);
    expect(diffLines(undefined)).toEqual([]);
  });

  it("shows before and after for {from, to}", () => {
    expect(diffLines({ quantity: { from: 500, to: 120 } })).toEqual([{ key: "quantity", before: "500", after: "120" }]);
  });

  it("shows none for a null side", () => {
    expect(diffLines({ supplier: { from: null, to: "Acme" } })).toEqual([{ key: "supplier", before: "none", after: "Acme" }]);
  });

  it("keeps strings unquoted and objects as JSON", () => {
    expect(diffLines({ x: { from: "a", to: { n: 1 } } })).toEqual([{ key: "x", before: "a", after: '{"n":1}' }]);
  });

  it("reads {chose}", () => {
    expect(diffLines({ conflict: { chose: "system" } })).toEqual([{ key: "conflict", note: "chose system" }]);
  });

  it("falls back to a note for plain values and hashes", () => {
    expect(diffLines({ status: "APPROVED", count: 3, hash: { a: 1 } })).toEqual([
      { key: "status", note: "APPROVED" },
      { key: "count", note: "3" },
      { key: "hash", note: '{"a":1}' },
    ]);
  });

  it("does not crash on a non-object diff", () => {
    expect(diffLines("raw text")).toEqual([{ key: "change", note: "raw text" }]);
    expect(diffLines(42)).toEqual([{ key: "change", note: "42" }]);
    expect(diffLines([1, 2])).toEqual([{ key: "change", note: "[1,2]" }]);
  });

  it("treats an array value as a note, not a before/after", () => {
    expect(diffLines({ ids: [1, 2] })).toEqual([{ key: "ids", note: "[1,2]" }]);
  });
});
