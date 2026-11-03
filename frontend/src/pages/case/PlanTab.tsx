import type { Policy, Recommendation } from "../../api/types";
import { Chip, Empty, Notice, Panel } from "../../components/ui";
import { currentRecommendation } from "../../lib/case";
import { fmtInr, humanize } from "../../lib/format";
import { gateView, POLICY_RULE, roleLabel, WORKING, type Tone } from "../../lib/labels";
import { DecisionPanel } from "./DecisionPanel";
import type { CaseTabProps } from "./shared";

const REC_STATUS: Record<Recommendation["status"], { label: string; tone: Tone }> = {
  proposed: { label: "Proposed", tone: "person" },
  approved: { label: "Approved", tone: "ok" },
  rejected: { label: "Rejected", tone: "bad" },
  superseded: { label: "Superseded by a newer plan", tone: "neutral" },
  abstained: { label: "AI abstained", tone: "person" },
};

export function PlanTab({ detail, user }: CaseTabProps) {
  const rec = currentRecommendation(detail.recommendations);
  const status = detail.case.status;

  if (!rec) {
    return (
      <Panel>
        <Empty title="No plan yet">
          {WORKING.has(status)
            ? "The AI is still working. This page updates by itself."
            : status === "NEEDS_INFO"
              ? "The plan starts once the missing or conflicting facts are settled. See the Issues tab."
              : "No plan was drafted for this case."}
        </Empty>
      </Panel>
    );
  }

  const st = REC_STATUS[rec.status];
  const fromAi = rec.evidence.rationale_source === "llm";
  const gates = (rec.evidence.gate_events ?? []).map(gateView);

  return (
    <>
      <DecisionPanel detail={detail} user={user} />

      <Panel
        title="Recommended plan"
        id="plan"
        aside={
          <span className="chips">
            <Chip tone={st.tone}>{st.label}</Chip>
            <Chip>Round {rec.round}</Chip>
          </span>
        }
      >
        {rec.status === "abstained" ? (
          <Notice tone="person" title="The AI chose not to guess">
            The evidence was too thin or in conflict, so there is no costed option. A person decides what to do next.
          </Notice>
        ) : (
          <dl className="kv">
            <div>
              <dt>Chosen action</dt>
              <dd>{rec.chosen_option ? humanize(rec.chosen_option) : "none"}</dd>
            </div>
            <div>
              <dt>Extra cost</dt>
              <dd className="num">{fmtInr(rec.total_cost)}</dd>
            </div>
            <div>
              <dt>Inputs used</dt>
              <dd className="num">
                {rec.inputs_used} of {rec.inputs_total}
              </dd>
            </div>
            <div>
              <dt>Evidence</dt>
              <dd>{humanize(rec.sufficiency)}</dd>
            </div>
          </dl>
        )}

        {rec.rationale ? (
          <>
            <h3>
              Why this plan{" "}
              <Chip tone={fromAi ? "instrument" : "neutral"} title={fromAi ? "Written by the AI, then checked by code" : "The AI was unavailable or failed a check, so a fixed template wrote this"}>
                {fromAi ? "AI wrote this" : "Template wrote this"}
              </Chip>
            </h3>
            <p>{rec.rationale}</p>
          </>
        ) : null}

        {rec.evidence.risks_of_choice && rec.evidence.risks_of_choice.length > 0 ? (
          <>
            <h3>Risks of this choice</h3>
            <ul>
              {rec.evidence.risks_of_choice.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </>
        ) : null}
      </Panel>

      {rec.options.length > 0 ? (
        <Panel title="Options considered" id="options">
          <p className="muted">Code computes every number. The AI only picks among these options and explains the pick.</p>
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Options considered for this case</caption>
              <thead>
                <tr>
                  <th scope="col">Action</th>
                  <th scope="col">What it means</th>
                  <th scope="col">Units</th>
                  <th scope="col">Lead time</th>
                  <th scope="col">Extra cost</th>
                  <th scope="col">Covers shortfall</th>
                  <th scope="col">Meets need date</th>
                </tr>
              </thead>
              <tbody>
                {rec.options.map((o) => (
                  <tr key={o.action}>
                    <th scope="row">
                      {humanize(o.action)} {o.action === rec.chosen_option ? <Chip tone="person">Chosen</Chip> : null}
                      {o.last_resort ? <span className="table__sub">Last resort</span> : null}
                      {o.substitute ? <span className="table__sub">Different product</span> : null}
                    </th>
                    <td>{o.description}</td>
                    <td className="num">{o.units ?? "none"}</td>
                    <td className="num">{o.lead_time_days === null || o.lead_time_days === undefined ? "none" : `${o.lead_time_days} d`}</td>
                    <td className="num">
                      {fmtInr(o.additional_cost)}
                      {o.spend_basis === "new_purchase" ? <span className="table__sub">New purchase</span> : null}
                      {o.spend_basis === "premium_over_po" ? <span className="table__sub">Premium over the open order</span> : null}
                    </td>
                    <td>{yesNo(o.covers_shortfall)}</td>
                    <td>{yesNo(o.feasible_by_need)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}

      <WhyHuman policy={detail.policy} />

      {gates.length > 0 || fromAi ? (
        <Panel title="Checks on the AI's output" id="gates">
          <p className="muted">Code checks every AI answer before it is used. When a check rejects the answer, it is dropped and code or a person takes over.</p>
          {gates.length === 0 ? (
            <p>No check rejected the AI's wording for this plan.</p>
          ) : (
            <ul className="gates">
              {gates.map((g, i) => (
                <li key={`${g.code}-${i}`}>
                  <Chip tone={g.kind === "blocked" ? "bad" : "neutral"}>{g.kind === "blocked" ? "Rejected" : "Fallback"}</Chip> {g.text}{" "}
                  <span className="muted num">{g.code}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      ) : null}
    </>
  );
}

function yesNo(v: boolean | undefined): string {
  return v === undefined ? "unknown" : v ? "Yes" : "No";
}

function WhyHuman({ policy }: { policy: Policy | null }) {
  if (!policy) {
    return (
      <Panel title="Why a person must approve" id="why">
        <p className="muted">The policy engine has not decided yet.</p>
      </Panel>
    );
  }
  const tiers = policy.inputs.tiers ?? [];
  return (
    <Panel title="Why a person must approve" id="why" aside={<Chip tone={policy.requires_human ? "person" : "neutral"}>{policy.requires_human ? "Person required" : "No person required"}</Chip>}>
      <p className="muted">Policy is plain code, not AI. Confidence can only add review, never remove it.</p>
      {policy.rules_fired.length === 0 ? (
        <p>No rule fired.</p>
      ) : (
        <ul className="rules">
          {policy.rules_fired.map((r) => (
            <li key={r.id}>
              <strong>{POLICY_RULE[r.id] ?? humanize(r.id)}</strong>
              <span className="muted"> {r.why}</span>
            </li>
          ))}
        </ul>
      )}
      {tiers.length > 0 ? (
        <p>
          Approvals needed: {tiers.map((t) => roleLabel(t.role)).join(" and ")}
          {policy.inputs.amount !== undefined ? (
            <>
              {" "}
              for <span className="num">{fmtInr(policy.inputs.amount)}</span>
            </>
          ) : null}
          .
        </p>
      ) : null}
    </Panel>
  );
}
