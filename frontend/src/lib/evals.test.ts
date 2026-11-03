import { describe, expect, it } from "vitest";
import type { EvalMode, EvalRate } from "../api/types";
import { bestMode, clamp01, countText, goldCount, pct, rangeText, rateText, staleText, weakPayloads } from "./evals";

const rate = (k: number, n: number, lo = 0.1, hi = 0.9): EvalRate => ({ k, n, rate: n ? k / n : null, ci95: [lo, hi] });
const mode = (scored: number) => ({ scored }) as EvalMode;

describe("rate text", () => {
  it("formats a measured rate, its count and its interval", () => {
    expect(rateText(rate(27, 48))).toBe("56%");
    expect(countText(rate(27, 48))).toBe("27 of 48");
    expect(rangeText(rate(27, 48, 0.4249, 0.6906))).toBe("between 42% and 69%");
  });
  it("never invents a number when nothing was measured", () => {
    expect(rateText(rate(0, 0))).toBe("none");
    expect(rangeText(rate(0, 0))).toBe("no data");
    expect(rateText(null)).toBe("none");
    expect(pct(undefined)).toBe("none");
    expect(pct(Number.NaN)).toBe("none");
  });
});

describe("clamp01", () => {
  it("keeps a bar inside its track", () => {
    expect(clamp01(1.4)).toBe(1);
    expect(clamp01(-2)).toBe(0);
    expect(clamp01(null)).toBe(0);
    expect(clamp01(0.5)).toBe(0.5);
  });
});

describe("bestMode", () => {
  it("prefers the fullest pipeline that was actually scored", () => {
    expect(bestMode({ "llm+second": mode(26), llm: mode(26), rules_only: mode(26) })).toBe("llm+second");
    expect(bestMode({ "llm+second": mode(0), llm: mode(12), rules_only: mode(26) })).toBe("llm");
    expect(bestMode({ "llm+second": mode(0), llm: mode(0), rules_only: mode(26) })).toBe("rules_only");
    expect(bestMode({ llm: mode(0) })).toBeNull();
  });
});

describe("goldCount", () => {
  it("counts emails that state the value: a wrong value sits in fn once, so tp + fn is the total", () => {
    expect(goldCount({ tp: 10, fn: 4 })).toBe(14);
  });
});

describe("staleText", () => {
  it("names what changed in plain words", () => {
    expect(staleText(["prompt_hash"])).toContain("the AI instructions");
    expect(staleText(["prompt_hash", "dataset_hash"])).toContain("the AI instructions and the test emails");
  });
});

describe("weakPayloads", () => {
  it("lists only payloads the scanner missed, weakest first", () => {
    const g = (r: EvalRate) => ({ detected: r }) as never;
    const out = weakPayloads({ H1: g(rate(8, 8)), H2: g(rate(0, 8)), H4: g(rate(4, 8)) });
    expect(out.map((x) => x.id)).toEqual(["H2", "H4"]);
  });
});
