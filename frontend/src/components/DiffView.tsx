import { diffLines } from "../lib/diff";
import { humanize } from "../lib/format";

/** Before and after, side by side in words. Colour marks removed and added; the words carry the meaning too. */
export function DiffView({ diff }: { diff: unknown }) {
  const lines = diffLines(diff);
  if (lines.length === 0) return null;
  return (
    <ul className="diff">
      {lines.map((l) => (
        <li key={l.key}>
          <span className="diff__key">{humanize(l.key)}</span>{" "}
          {l.note !== undefined ? (
            <span className="num">{l.note}</span>
          ) : (
            <>
              <span className="diff__from num" aria-label="before">
                {l.before}
              </span>
              <span aria-hidden="true"> to </span>
              <span className="visually-hidden"> changed to </span>
              <span className="diff__to num" aria-label="after">
                {l.after}
              </span>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}
