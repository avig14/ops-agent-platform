import { useMemo } from "react";
import { Link, useSearchParams } from "react-router";
import { useCases } from "../api/hooks";
import type { CaseRow } from "../api/types";
import { useUser } from "../components/Auth";
import { Boundary, Chip, Empty, PageHeader, Skeleton, StatusChip } from "../components/ui";
import { fmtDateTime, humanize, plural, timeLeft } from "../lib/format";
import { filterCases, sortCases, VIEWS } from "../lib/inbox";
import { FINISHED, RISK_TONE } from "../lib/labels";

export function InboxPage() {
  const user = useUser();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") ?? "all";
  const q = params.get("q") ?? "";
  const cases = useCases(undefined);
  const canCreate = user.role !== "admin";

  function setParam(key: string, value: string) {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value && !(key === "view" && value === "all")) next.set(key, value);
        else next.delete(key);
        return next;
      },
      { replace: true },
    );
  }

  const counts = useMemo(() => {
    const rows = cases.data ?? [];
    return Object.fromEntries(VIEWS.map((v) => [v.key, rows.filter(v.test).length]));
  }, [cases.data]);

  return (
    <div className="page">
      <PageHeader
        title="Inbox"
        lead="Requests the AI has read. People are asked only where a decision or a missing fact is needed."
        actions={
          canCreate ? (
            <Link to="/new" className="btn btn--primary">
              New request
            </Link>
          ) : null
        }
      />

      <div className="toolbar">
        <div className="segmented" role="group" aria-label="Filter by state">
          {VIEWS.map((v) => (
            <button
              key={v.key}
              type="button"
              className={`segmented__btn${view === v.key ? " segmented__btn--on" : ""}`}
              aria-pressed={view === v.key}
              onClick={() => setParam("view", v.key)}
            >
              {v.label} <span className="num">{counts[v.key] ?? 0}</span>
            </button>
          ))}
        </div>
        <div className="field field--inline">
          <label htmlFor="q">Search</label>
          <input id="q" type="search" value={q} placeholder="Case id, title or region" onChange={(e) => setParam("q", e.target.value)} />
        </div>
      </div>

      <Boundary
        query={cases}
        what="cases"
        skeleton={<Skeleton rows={6} label="Loading cases" />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <Empty
            title="No requests yet"
            action={
              canCreate ? (
                <Link to="/new" className="btn btn--primary">
                  Create the first request
                </Link>
              ) : null
            }
          >
            {canCreate ? "Paste an email or load a ready-made scenario to see the agents work." : "Nothing has been submitted yet."}
          </Empty>
        }
      >
        {(rows) => {
          const shown = sortCases(filterCases(rows, view, q));
          if (shown.length === 0) {
            return (
              <Empty
                title="Nothing matches"
                action={
                  <button type="button" className="btn" onClick={() => setParams({}, { replace: true })}>
                    Clear filters
                  </button>
                }
              >
                {plural(rows.length, "case")} exist, none in this view.
              </Empty>
            );
          }
          return (
            <div className="table-wrap">
              <table className="table table--rows">
                <caption className="visually-hidden">Cases, most urgent first</caption>
                <thead>
                  <tr>
                    <th scope="col">Case</th>
                    <th scope="col">State</th>
                    <th scope="col">Priority</th>
                    <th scope="col">Risk</th>
                    <th scope="col">Flags</th>
                    <th scope="col">Due</th>
                    <th scope="col">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((c) => (
                    <CaseTableRow key={c.id} c={c} />
                  ))}
                </tbody>
              </table>
            </div>
          );
        }}
      </Boundary>
    </div>
  );
}

function CaseTableRow({ c }: { c: CaseRow }) {
  const due = FINISHED.has(c.status) ? null : timeLeft(c.deadline_at);
  return (
    <tr>
      <th scope="row">
        <Link to={`/cases/${c.id}`} className="rowlink">
          {c.title ?? (c.intent ? humanize(c.intent) : "Untitled request")}
        </Link>
        <span className="table__sub num">
          {c.id}
          {c.region ? ` ${c.region}` : ""}
        </span>
      </th>
      <td>
        <StatusChip status={c.status} />
      </td>
      <td className="num">{c.priority ?? "none"}</td>
      <td>{c.risk_level ? <Chip tone={RISK_TONE[c.risk_level] ?? "neutral"}>{c.risk_level}</Chip> : <span className="muted">not scored</span>}</td>
      <td>
        <span className="chips">
          {c.injection_flag ? <Chip tone="bad" title="The request tried to instruct the system">Injection flagged</Chip> : null}
          {c.degraded ? <Chip tone="person" title="A dependency was down; analysis is incomplete">Degraded</Chip> : null}
          {c.duplicate_of ? <Chip>Duplicate of {c.duplicate_of}</Chip> : null}
          {c.parent_id ? <Chip>Child of {c.parent_id}</Chip> : null}
        </span>
      </td>
      <td className={due?.overdue ? "text-bad" : undefined}>{due ? due.text : <span className="muted">none</span>}</td>
      <td className="num">{fmtDateTime(c.updated_at)}</td>
    </tr>
  );
}
