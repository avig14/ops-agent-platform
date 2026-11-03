import { useDecision } from "../../api/hooks";
import { ActionError, Chip, Panel } from "../../components/ui";
import { Confirm } from "../../components/Confirm";
import { dueLabel, fmtInr, timeLeft } from "../../lib/format";
import { roleLabel } from "../../lib/labels";
import { approvalHint } from "../../lib/permissions";
import { approvedBy, pendingDecisions } from "../../lib/case";
import type { CaseTabProps } from "./shared";

/**
 * Approve or reject. Buttons stay on for anyone who can see the case: if the wrong person tries, the server refuses,
 * writes the refusal to the audit trail and this screen shows why. The hint below says it up front.
 */
export function DecisionPanel({ detail, user }: CaseTabProps) {
  const c = detail.case;
  const decide = useDecision(c.id);
  const pending = pendingDecisions(detail);

  return (
    <Panel title="Decision" id="decision" aside={decide.isPending ? <Chip tone="instrument">Recording</Chip> : null}>
      {pending.length === 0 ? (
        <p className="muted">No approval is waiting. {c.status === "APPROVED" ? "All required approvals are in." : "A decision appears here once a plan is ready."}</p>
      ) : (
        <ul className="decisions">
          {pending.map((a) => {
            const hint = approvalHint(user, a, c, approvedBy(detail.approvals, a.round));
            const due = timeLeft(a.deadline_at);
            return (
              <li key={a.id} className="decision">
                <p className="decision__who">
                  <strong>{roleLabel(a.required_role)}</strong> must decide on <span className="num">{fmtInr(a.amount)}</span>
                  {due ? (
                    <>
                      {" "}
                      <span className={due.overdue ? "text-bad" : "muted"}>{dueLabel(due, "Deadline")}</span>
                    </>
                  ) : null}
                </p>
                {hint ? (
                  <p className="field__hint">
                    {hint} You can still try: the server refuses and records it.
                  </p>
                ) : null}
                <div className="decision__actions">
                  <Confirm
                    label="Approve"
                    tone="primary"
                    prompt={`Approve ${fmtInr(a.amount)} as ${roleLabel(user.role).toLowerCase()}? This is recorded under your name.`}
                    confirmLabel="Yes, approve"
                    pending={decide.isPending}
                    onConfirm={() => decide.mutate({ approvalId: a.id, decision: "approve" })}
                  />
                  <Confirm
                    label="Reject"
                    tone="danger"
                    prompt="Reject this plan? The AI will draft a different one without this option."
                    confirmLabel="Yes, reject"
                    reasonLabel="Why are you rejecting it? (needed, at least 3 characters)"
                    minReason={3}
                    pending={decide.isPending}
                    onConfirm={(reason) => decide.mutate({ approvalId: a.id, decision: "reject", reason })}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <ActionError error={decide.error} what="record the decision" />
    </Panel>
  );
}
