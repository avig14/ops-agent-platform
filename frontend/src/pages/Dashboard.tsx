import { Link } from "react-router";
import { useNarrative, useReport } from "../api/hooks";
import { ActionError, Boundary, Chip, Empty, PageHeader, Panel, Skeleton, Stat } from "../components/ui";
import { fmtDateTime, fmtHours, fmtInr, humanize } from "../lib/format";
import { statusInfo } from "../lib/labels";

export function DashboardPage() {
  const report = useReport(true);
  const narrative = useNarrative();

  return (
    <div className="page">
      <PageHeader title="Management dashboard" lead="Numbers come straight from the database. The AI summary may only repeat these numbers; code rejects any figure it invents." />
      <Boundary
        query={report}
        what="the report"
        skeleton={<Skeleton rows={8} label="Loading the report" />}
        isEmpty={(r) => r.cases.total === 0}
        empty={
          <Empty
            title="Nothing to report yet"
            action={
              <Link to="/new" className="btn btn--primary">
                Create a request
              </Link>
            }
          >
            Numbers appear after the first request is submitted.
          </Empty>
        }
      >
        {(r) => (
          <>
            <Panel title="Where things stand" id="standing" aside={<span className="muted">{r.scope}, as of <span className="num">{fmtDateTime(r.generated_at)}</span></span>}>
              <dl className="stats">
                <Stat label="Open cases" value={r.cases.open} note={`${r.cases.total} in total`} />
                <Stat label="Need a person" value={r.needs_human} tone={r.needs_human > 0 ? "person" : undefined} />
                <Stat label="Open P1" value={r.open_p1} tone={r.open_p1 > 0 ? "person" : undefined} />
                <Stat label="Approvals waiting" value={r.approvals.pending} note={r.approvals.overdue > 0 ? `${r.approvals.overdue} overdue` : "none overdue"} tone={r.approvals.overdue > 0 ? "bad" : undefined} />
                <Stat label="Approved spend" value={fmtInr(r.spend_inr.approved)} />
                <Stat label="Spend awaiting approval" value={fmtInr(r.spend_inr.pending_approval)} />
                <Stat label="Average decision time" value={fmtHours(r.approvals.avg_decision_hours)} />
                <Stat label="Average time to close" value={fmtHours(r.closure.avg_cycle_hours)} note={`${r.closure.reopened} reopened`} />
              </dl>
            </Panel>

            <div className="split split--even">
              <Panel title="Open cases by risk" id="risk">
                {Object.keys(r.open_by_risk).length === 0 ? (
                  <p className="muted">No open cases.</p>
                ) : (
                  <dl className="stats">
                    {Object.entries(r.open_by_risk).map(([k, n]) => (
                      <Stat key={k} label={k} value={n} />
                    ))}
                  </dl>
                )}
              </Panel>
              <Panel title="Safety and delivery" id="safety">
                <dl className="stats">
                  <Stat label="Injection flagged" value={r.injection_flagged} tone={r.injection_flagged > 0 ? "bad" : undefined} />
                  <Stat label="Degraded analysis" value={r.degraded} tone={r.degraded > 0 ? "person" : undefined} />
                  <Stat label="Messages sent" value={r.comms.sent} />
                  <Stat label="Waiting or draft" value={r.comms.waiting + r.comms.drafts} />
                  <Stat label="Failing" value={r.comms.failed} tone={r.comms.failed > 0 ? "bad" : undefined} />
                  <Stat label="Undeliverable" value={r.comms.dead} tone={r.comms.dead > 0 ? "bad" : undefined} />
                </dl>
              </Panel>
            </div>

            <Panel title="Cases by state" id="states">
              <div className="table-wrap">
                <table className="table">
                  <caption className="visually-hidden">Number of cases in each state</caption>
                  <thead>
                    <tr>
                      <th scope="col">State</th>
                      <th scope="col">Cases</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(r.cases.by_status).map(([s, n]) => (
                      <tr key={s}>
                        <th scope="row">{statusInfo(s).label}</th>
                        <td className="num">{n}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>

            <Panel title="Needs attention" id="attention">
              {r.attention.length === 0 ? (
                <p className="muted">Nothing is stuck, overdue or flagged.</p>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <caption className="visually-hidden">Cases that need attention, worst first</caption>
                    <thead>
                      <tr>
                        <th scope="col">Case</th>
                        <th scope="col">Why</th>
                      </tr>
                    </thead>
                    <tbody>
                      {r.attention.map((a) => (
                        <tr key={a.case_id}>
                          <th scope="row">
                            <Link to={`/cases/${a.case_id}`} className="rowlink">
                              {a.title ?? "Untitled request"}
                            </Link>
                            <span className="table__sub num">{a.case_id}</span>
                          </th>
                          <td>
                            <span className="chips">
                              {a.reasons.map((w) => (
                                <Chip key={w} tone={w.includes("overdue") || w.includes("injection") || w.includes("undeliverable") ? "bad" : "person"}>
                                  {humanize(w)}
                                </Chip>
                              ))}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>
          </>
        )}
      </Boundary>

      <Panel
        title="Management summary"
        id="narrative"
        aside={
          narrative.data ? (
            <Chip tone={narrative.data.source === "llm" ? "instrument" : "neutral"}>{narrative.data.source === "llm" ? "AI wrote this" : "Template wrote this"}</Chip>
          ) : null
        }
      >
        <p className="muted">One paragraph for people who will not open every case. Generated on request, checked against the numbers above.</p>
        {narrative.data ? (
          <>
            <p className="summary__headline">{narrative.data.content.headline}</p>
            <p>{narrative.data.content.what_happened}</p>
            {narrative.data.content.decisions.length > 0 ? (
              <>
                <h3>Decisions waiting</h3>
                <ul>
                  {narrative.data.content.decisions.map((d) => (
                    <li key={d}>{d}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {narrative.data.content.open_items.length > 0 ? (
              <>
                <h3>Needs attention</h3>
                <ul>
                  {narrative.data.content.open_items.map((d) => (
                    <li key={d}>{d}</li>
                  ))}
                </ul>
              </>
            ) : null}
            <p className="muted">
              Written <span className="num">{fmtDateTime(narrative.data.generated_at)}</span>.
              {narrative.data.source === "template" ? " The AI was unavailable or failed a check, so a fixed template wrote it." : ""}
            </p>
          </>
        ) : null}
        <button type="button" className="btn" disabled={narrative.isPending} onClick={() => narrative.mutate()}>
          {narrative.isPending ? "Writing" : narrative.data ? "Write it again" : "Write the summary"}
        </button>
        <ActionError error={narrative.error} what="write the summary" />
      </Panel>
    </div>
  );
}
