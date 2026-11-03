import { useMemo } from "react";
import { useSearchParams } from "react-router";
import { useAudit } from "../../api/hooks";
import type { AuditEvent } from "../../api/types";
import { ActorBadge, Boundary, Chip, Empty, Panel, Skeleton } from "../../components/ui";
import { DiffView } from "../../components/DiffView";
import { AUDIT_FILTERS, matchesFilter, parseAuditFilter, type AuditFilter } from "../../lib/audit";
import { fmtDateTime, humanize, plural, shortHash } from "../../lib/format";
import type { Tone } from "../../lib/labels";
import type { CaseTabProps } from "./shared";

const OUTCOME: Record<string, Tone> = { success: "ok", failure: "bad", blocked: "bad", degraded: "person", skipped: "neutral", attempt: "instrument" };

export function AuditTab({ detail }: CaseTabProps) {
  const audit = useAudit(detail.case.id);
  const [params, setParams] = useSearchParams();
  const filter = parseAuditFilter(params.get("show"));

  function setFilter(next: AuditFilter) {
    setParams(
      (prev) => {
        const n = new URLSearchParams(prev);
        if (next === "all") n.delete("show");
        else n.set("show", next);
        return n;
      },
      { replace: true },
    );
  }

  const events = audit.data?.events;
  const shown = useMemo(() => (events ?? []).filter((e) => matchesFilter(e, filter)), [events, filter]);

  return (
    <Boundary
      query={audit}
      what="the audit trail"
      skeleton={<Skeleton rows={6} label="Loading the audit trail" />}
      isEmpty={(d) => d.events.length === 0}
      empty={
        <Panel>
          <Empty title="Nothing recorded yet">Every AI step and every person's action is written here as it happens.</Empty>
        </Panel>
      }
    >
      {(d) => (
        <>
          <Panel
            title="Tamper check"
            id="chain"
            aside={<Chip tone={d.chain.ok ? "ok" : "bad"}>{d.chain.ok ? "Chain intact" : "Chain broken"}</Chip>}
          >
            <p>
              Each entry carries a hash of the one before it. Editing or deleting any entry breaks every hash after it, and this check finds
              it. It proves tampering is detectable, not impossible.
            </p>
            <dl className="kv">
              <div>
                <dt>Entries checked</dt>
                <dd className="num">{d.chain.checked}</dd>
              </div>
              <div>
                <dt>Latest hash</dt>
                <dd className="num" title={d.chain.head_hash ?? undefined}>
                  {shortHash(d.chain.head_hash, 16)}
                </dd>
              </div>
              {d.chain.ok ? null : (
                <div>
                  <dt>First bad entry</dt>
                  <dd className="num text-bad">
                    {d.chain.first_bad_seq ?? "unknown"}: {d.chain.problem ?? "hash mismatch"}
                  </dd>
                </div>
              )}
            </dl>
          </Panel>

          <Panel title="Audit trail" id="trail" aside={<span className="muted">{plural(shown.length, "entry", "entries")}</span>}>
            <div className="segmented" role="group" aria-label="Filter the audit trail">
              {AUDIT_FILTERS.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  className={`segmented__btn${filter === f.key ? " segmented__btn--on" : ""}`}
                  aria-pressed={filter === f.key}
                  onClick={() => setFilter(f.key)}
                >
                  {f.label}
                </button>
              ))}
            </div>
            {shown.length === 0 ? (
              <Empty
                title="Nothing matches"
                action={
                  <button type="button" className="btn" onClick={() => setFilter("all")}>
                    Show everything
                  </button>
                }
              >
                No entry in this filter for this case.
              </Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <caption className="visually-hidden">Audit trail, oldest first</caption>
                  <thead>
                    <tr>
                      <th scope="col">No.</th>
                      <th scope="col">Who</th>
                      <th scope="col">What happened</th>
                      <th scope="col">Result</th>
                      <th scope="col">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map((e) => (
                      <AuditRow key={e.seq} e={e} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </>
      )}
    </Boundary>
  );
}

function AuditRow({ e }: { e: AuditEvent }) {
  return (
    <tr>
      <th scope="row" className="num">
        {e.seq}
      </th>
      <td>
        <ActorBadge type={e.actor_type} />
        <span className="table__sub">{e.actor_id}</span>
      </td>
      <td>
        <p>{e.summary}</p>
        <p className="muted">
          {humanize(e.action)}
          {e.reason_code ? <span className="num"> {e.reason_code}</span> : null}
          {e.model ? <span className="num"> {e.model}</span> : null}
        </p>
        <DiffView diff={e.diff} />
        {e.error ? (
          <p className="text-bad">
            Error {e.error.class ?? ""} {e.error.msg ?? ""}
            {e.error.http_status ? <span className="num"> HTTP {e.error.http_status}</span> : null}
          </p>
        ) : null}
        {e.not_fetched && e.not_fetched.length > 0 ? (
          <p className="muted">
            Not fetched:{" "}
            {e.not_fetched.map((n) => `${n.source}${n.reason_code ? ` (${n.reason_code})` : ""}`).join(", ")}
          </p>
        ) : null}
        <details className="details">
          <summary>Technical detail</summary>
          <dl className="kv">
            <div>
              <dt>Entry hash</dt>
              <dd className="num">{shortHash(e.row_hash, 24)}</dd>
            </div>
            <div>
              <dt>Trace id</dt>
              <dd className="num">{e.trace_id ?? "none"}</dd>
            </div>
            <div>
              <dt>Severity</dt>
              <dd>{humanize(e.severity)}</dd>
            </div>
            {e.sim_ts ? (
              <div>
                <dt>Simulated time</dt>
                <dd className="num">{fmtDateTime(e.sim_ts)}</dd>
              </div>
            ) : null}
          </dl>
        </details>
      </td>
      <td>
        <Chip tone={OUTCOME[e.outcome] ?? "neutral"}>{humanize(e.outcome)}</Chip>
      </td>
      <td className="num">{fmtDateTime(e.ts)}</td>
    </tr>
  );
}
