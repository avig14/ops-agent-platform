import { describe, expect, it } from "vitest";
import { splitByQuotes } from "./quotes";

const joined = (s: ReturnType<typeof splitByQuotes>) => s.map((x) => x.text).join("");
const hits = (s: ReturnType<typeof splitByQuotes>) => s.filter((x) => x.hit).map((x) => x.text);

const TEXT = "Hi team, the PO for 500 units from Acme is delayed by 6 days. Need it by 12 Nov.";

describe("splitByQuotes", () => {
  it("returns one plain segment when there are no quotes", () => {
    expect(splitByQuotes(TEXT, [])).toEqual([{ text: TEXT, hit: false }]);
  });

  it("marks a quote and keeps the original text intact", () => {
    const s = splitByQuotes(TEXT, ["500 units"]);
    expect(hits(s)).toEqual(["500 units"]);
    expect(joined(s)).toBe(TEXT);
  });

  it("matches ignoring case but keeps the original casing in the segment", () => {
    const s = splitByQuotes(TEXT, ["ACME"]);
    expect(hits(s)).toEqual(["Acme"]);
  });

  it("marks every occurrence", () => {
    const s = splitByQuotes("a b a b a", ["a"]);
    expect(hits(s)).toEqual(["a", "a", "a"]);
    expect(joined(s)).toBe("a b a b a");
  });

  it("merges overlapping quotes into one highlight", () => {
    const s = splitByQuotes("the quick brown fox", ["quick brown", "brown fox"]);
    expect(hits(s)).toEqual(["quick brown fox"]);
  });

  it("merges touching quotes", () => {
    const s = splitByQuotes("abcdef", ["abc", "def"]);
    expect(hits(s)).toEqual(["abcdef"]);
  });

  it("ignores quotes that are not in the text instead of inventing a highlight", () => {
    const s = splitByQuotes(TEXT, ["Chennai", "999 units"]);
    expect(s).toEqual([{ text: TEXT, hit: false }]);
  });

  it("ignores null, undefined, empty and blank quotes", () => {
    expect(splitByQuotes(TEXT, [null, undefined, "", "   "])).toEqual([{ text: TEXT, hit: false }]);
  });

  it("handles a quote at the very start and very end", () => {
    const s = splitByQuotes("start middle end", ["start", "end"]);
    expect(s[0]).toEqual({ text: "start", hit: true });
    expect(s[s.length - 1]).toEqual({ text: "end", hit: true });
    expect(joined(s)).toBe("start middle end");
  });

  it("handles empty text", () => {
    expect(splitByQuotes("", ["x"])).toEqual([{ text: "", hit: false }]);
  });

  it("treats regex characters as plain text", () => {
    const s = splitByQuotes("cost (approx.) is 5+5", ["(approx.)", "5+5"]);
    expect(hits(s)).toEqual(["(approx.)", "5+5"]);
  });
});
