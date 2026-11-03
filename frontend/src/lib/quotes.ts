export interface Segment {
  text: string;
  hit: boolean;
}

/**
 * Split `text` so every place a quote appears (case-insensitive) is its own `hit` segment.
 * Overlapping or touching matches merge. Quotes that are not found are ignored, never invented.
 */
export function splitByQuotes(text: string, quotes: readonly (string | null | undefined)[]): Segment[] {
  const lower = text.toLowerCase();
  const ranges: [number, number][] = [];
  for (const raw of quotes) {
    const q = raw?.trim().toLowerCase();
    if (!q) continue;
    let from = 0;
    for (;;) {
      const at = lower.indexOf(q, from);
      if (at < 0) break;
      ranges.push([at, at + q.length]);
      from = at + q.length;
    }
  }
  if (ranges.length === 0) return [{ text, hit: false }];

  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }

  const out: Segment[] = [];
  let pos = 0;
  for (const [s, e] of merged) {
    if (s > pos) out.push({ text: text.slice(pos, s), hit: false });
    out.push({ text: text.slice(s, e), hit: true });
    pos = e;
  }
  if (pos < text.length) out.push({ text: text.slice(pos), hit: false });
  return out;
}
