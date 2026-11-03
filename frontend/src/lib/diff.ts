/** Turns the audit `diff` blob into lines a person can read. The server writes {field: {from, to}}, {field: {chose}} or small hashes. */

export interface DiffLine {
  key: string;
  before?: string;
  after?: string;
  note?: string;
}

function show(v: unknown): string {
  if (v === null || v === undefined) return "none";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

export function diffLines(diff: unknown): DiffLine[] {
  if (diff === null || diff === undefined) return [];
  if (typeof diff !== "object" || Array.isArray(diff)) return [{ key: "change", note: show(diff) }];
  return Object.entries(diff as Record<string, unknown>).map(([key, val]): DiffLine => {
    if (val !== null && typeof val === "object" && !Array.isArray(val)) {
      const o = val as Record<string, unknown>;
      if ("from" in o || "to" in o) return { key, before: show(o["from"]), after: show(o["to"]) };
      if ("chose" in o) return { key, note: `chose ${show(o["chose"])}` };
    }
    return { key, note: show(val) };
  });
}
