import { describe, expect, it } from "vitest";
import { ageText, dueLabel, fmtDate, fmtDateTime, fmtHours, fmtInr, fmtNumber, fmtPct, humanize, plural, shortHash, timeLeft } from "./format";

describe("dueLabel", () => {
  it("reads naturally in all three states, never doubling the word", () => {
    expect(dueLabel({ text: "in 3 h", overdue: false })).toBe("Due in 3 h");
    expect(dueLabel({ text: "overdue by 1 h 19 min", overdue: true })).toBe("Overdue by 1 h 19 min");
    expect(dueLabel({ text: "due now", overdue: false })).toBe("Due now");
    expect(dueLabel({ text: "in 3 h", overdue: false }, "Deadline")).toBe("Deadline in 3 h");
    expect(dueLabel({ text: "overdue by 2 h", overdue: true }, "Deadline")).toBe("Overdue by 2 h");
  });
});

const NOW = Date.parse("2026-11-03T10:00:00Z");
const at = (iso: string) => timeLeft(iso, NOW);

describe("fmtInr", () => {
  it("groups the Indian way", () => {
    expect(fmtInr(228000)).toBe("INR 2,28,000");
    expect(fmtInr(50000)).toBe("INR 50,000");
    expect(fmtInr(1234567)).toBe("INR 12,34,567");
  });
  it("accepts numeric strings (the API sends Decimal as string)", () => {
    expect(fmtInr("228000.00")).toBe("INR 2,28,000");
  });
  it("rounds to whole rupees", () => {
    expect(fmtInr(99.6)).toBe("INR 100");
  });
  it("says none for missing or junk, never NaN", () => {
    expect(fmtInr(null)).toBe("none");
    expect(fmtInr(undefined)).toBe("none");
    expect(fmtInr("abc")).toBe("none");
  });
  it("keeps zero as a real value", () => {
    expect(fmtInr(0)).toBe("INR 0");
  });
});

describe("fmtNumber and fmtPct", () => {
  it("formats with one decimal at most", () => {
    expect(fmtNumber(1234.56)).toBe("1,234.6");
    expect(fmtNumber(500)).toBe("500");
    expect(fmtNumber(null)).toBe("none");
    expect(fmtNumber(Number.NaN)).toBe("none");
  });
  it("rounds percent", () => {
    expect(fmtPct(0.5499)).toBe("55%");
    expect(fmtPct(1)).toBe("100%");
    expect(fmtPct(0)).toBe("0%");
  });
});

describe("fmtHours", () => {
  it("picks the unit a person would say", () => {
    expect(fmtHours(0.5)).toBe("30 min");
    expect(fmtHours(1.5)).toBe("1.5 h");
    expect(fmtHours(47)).toBe("47 h");
    expect(fmtHours(72)).toBe("3 d");
  });
  it("says none yet when there is no data", () => {
    expect(fmtHours(null)).toBe("none yet");
    expect(fmtHours(undefined)).toBe("none yet");
  });
});

describe("dates", () => {
  it("formats a real timestamp", () => {
    expect(fmtDateTime("2026-11-03T06:30:00Z")).toMatch(/Nov/);
    expect(fmtDate("2026-11-03T06:30:00Z")).toMatch(/2026/);
  });
  it("says none for missing or invalid input, never Invalid Date", () => {
    expect(fmtDateTime(null)).toBe("none");
    expect(fmtDateTime("")).toBe("none");
    expect(fmtDateTime("not a date")).toBe("none");
    expect(fmtDate(undefined)).toBe("none");
  });
});

describe("timeLeft", () => {
  it("counts down to the future", () => {
    expect(at("2026-11-03T13:20:00Z")).toEqual({ text: "in 3 h 20 min", overdue: false });
    expect(at("2026-11-03T10:45:00Z")).toEqual({ text: "in 45 min", overdue: false });
  });
  it("drops a zero tail", () => {
    expect(at("2026-11-03T13:00:00Z")?.text).toBe("in 3 h");
    expect(at("2026-11-04T10:00:00Z")?.text).toBe("in 1 d");
  });
  it("uses days once past 24 hours", () => {
    expect(at("2026-11-04T11:00:00Z")?.text).toBe("in 1 d 1 h");
  });
  it("flags overdue", () => {
    expect(at("2026-11-03T08:00:00Z")).toEqual({ text: "overdue by 2 h", overdue: true });
    expect(at("2026-11-03T09:30:00Z")).toEqual({ text: "overdue by 30 min", overdue: true });
  });
  it("is not overdue at the exact moment", () => {
    expect(at("2026-11-03T10:00:00Z")).toEqual({ text: "due now", overdue: false });
  });
  it("returns null with no deadline or a bad one", () => {
    expect(timeLeft(null, NOW)).toBeNull();
    expect(timeLeft("junk", NOW)).toBeNull();
  });
});

describe("ageText", () => {
  it("walks up the units", () => {
    expect(ageText(45)).toBe("45 s");
    expect(ageText(89)).toBe("89 s");
    expect(ageText(90)).toBe("2 min");
    expect(ageText(3600)).toBe("60 min");
    expect(ageText(5400)).toBe("2 h");
    expect(ageText(172800)).toBe("2 d");
  });
});

describe("text helpers", () => {
  it("humanizes enum words", () => {
    expect(humanize("expedite_alternate_supplier")).toBe("Expedite alternate supplier");
    expect(humanize("NEEDS-INFO")).toBe("Needs info");
    expect(humanize(null)).toBe("none");
    expect(humanize("")).toBe("none");
  });
  it("shortens hashes", () => {
    expect(shortHash("0123456789abcdef")).toBe("0123456789");
    expect(shortHash("0123456789abcdef", 4)).toBe("0123");
    expect(shortHash(null)).toBe("none");
  });
  it("pluralizes", () => {
    expect(plural(1, "case")).toBe("1 case");
    expect(plural(0, "case")).toBe("0 cases");
    expect(plural(2, "reply", "replies")).toBe("2 replies");
  });
});
