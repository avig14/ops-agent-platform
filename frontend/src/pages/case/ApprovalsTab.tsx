import { Chip, Empty, Panel } from "../../components/ui";
import { approvalsByRound } from "../../lib/case";
import { fmtDateTime, fmtInr, humanize, timeLeft } from "../../lib/format";
import { APPROVAL_TONE, roleLabel } from "../../lib/labels";
import type { Approval } from "../../api/types";
import { DecisionPanel } from "./DecisionPanel";
import type { CaseTabProps } from "./shared";

export function ApprovalsTab({ detail, user }: CaseTabProps) {
  const rounds = approvalsByRound(detail.approvals);
  const [latest, ...older] = rounds;

  return (
    <>
      <DecisionPanel detail={detail} user={user} />
      {!latest ? (
        <Panel>
          <Empty title="No approvals yet">Approvals are created when a plan with a cost is ready. Zero-cost, low-risk plans still go to a person for closure.</Empty>
        </Panel>
      ) : (
        <Panel title={`Approval ladder, round ${latest.round}`} id="ladder">
          <p className="muted">
            Higher amounts need more senior people. The person who submitted the request can never approve it. Nobody approves twice. The AI
            never approves. A missed deadline escalates; it is never auto-approved.
          </p>
          <LadderTable rows={latest.rows} />
        </Panel>
      )}
      {older.length > 0 ? (
        <Panel title="Earlier rounds" id="history">
          {older.map((g) => (
            <details key={g.round} className="details">
              <summary>
                Round {g.round}: {g.rows.map((r) => humanize(r.status)).join(", ")}
              </summary>
              <LadderTable rows={g.rows} />
            </details>
          ))}
        </Panel>
      ) : null}
    </>
  );
}

function LadderTable({ rows }: { rows: Approval[] }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <caption className="visually-hidden">Approvals in this round</caption>
        <thead>
          <tr>
            <th scope="col">Tier</th>
            <th scope="col">Who must decide</th>
            <th scope="col">State</th>
            <th scope="col">Decided by</th>
            <th scope="col">Amount</th>
            <th scope="col">Deadline</th>
            <th scope="col">Reminders</th>
            <th scope="col">Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => {
            const due = a.status === "pending" ? timeLeft(a.deadline_at) : null;
            return (
              <tr key={a.id}>
                <th scope="row" className="num">
                  {a.tier}
                </th>
                <td>{roleLabel(a.required_role)}</td>
                <td>
                  <Chip tone={APPROVAL_TONE[a.status]}>{humanize(a.status)}</Chip>
                </td>
                <td>
                  {a.approver_name ?? <span className="muted">nobody yet</span>}
                  {a.decided_at ? <span className="table__sub num">{fmtDateTime(a.decided_at)}</span> : null}
                </td>
                <td className="num">{fmtInr(a.amount)}</td>
                <td className={due?.overdue ? "text-bad" : undefined}>
                  {due ? due.text : a.deadline_at ? <span className="num">{fmtDateTime(a.deadline_at)}</span> : <span className="muted">none</span>}
                </td>
                <td>
                  {a.reminded_50 || a.reminded_90 ? (
                    <span className="chips">
                      {a.reminded_50 ? <Chip tone="instrument">50% sent</Chip> : null}
                      {a.reminded_90 ? <Chip tone="instrument">90% sent</Chip> : null}
                    </span>
                  ) : (
                    <span className="muted">none sent</span>
                  )}
                </td>
                <td>{a.reason ?? <span className="muted">none</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
