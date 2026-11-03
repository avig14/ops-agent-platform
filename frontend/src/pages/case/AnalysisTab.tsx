import type { ReactNode } from "react";
import { Chip, Empty, Notice, Panel, Stat } from "../../components/ui";
import { currentRecommendation } from "../../lib/case";
import { ageText, fmtDate, fmtInr, fmtNumber, humanize } from "../../lib/format";
import { RISK_TONE, WORKING } from "../../lib/labels";
import type { CaseTabProps } from "./shared";

function Reading({ title, source, level, impact, urgency, reasons }: { title: string; source: string; level?: string; impact?: string; urgency?: string; reasons: string[] }) {
  return (
    <div className="compare__col">
      <p className="compare__label">{title}</p>
      <p className="muted">{source}</p>
      <p className="compare__value">
        {level ? <Chip tone={RISK_TONE[level] ?? "neutral"}>{level} risk</Chip> : <span className="muted">no reading</span>}
      </p>
      <p className="muted">
        Impact {impact ?? "none"}, urgency {urgency ?? "none"}
      </p>
      {reasons.length > 0 ? (
        <ul>
          {reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function AnalysisTab({ detail }: CaseTabProps) {
  const f = detail.facts;
  const rec = currentRecommendation(detail.recommendations);
  const risk = rec?.evidence.risk;
  const disagree = !!risk?.conflict;

  if (!risk && !f.intake && f.shortfall === undefined) {
    return (
      <Panel>
        <Empty title="No analysis yet">{WORKING.has(detail.case.status) ? "The AI is still working. This page updates by itself." : "Nothing was analysed for this case."}</Empty>
      </Panel>
    );
  }

  const facts: [string, ReactNode][] = [
    ["Ordered", fmtNumber(f.qty_ordered)],
    ["On hand", fmtNumber(f.qty_available)],
    ["Shortfall", fmtNumber(f.shortfall)],
    ["Delay", f.delay_days === null || f.delay_days === undefined ? "none" : `${f.delay_days} d`],
    ["Days to need date", fmtNumber(f.days_to_need)],
    ["Launch gap", f.launch_gap_days === null || f.launch_gap_days === undefined ? "none" : `${f.launch_gap_days} d`],
    ["Needed by", fmtDate(f.needed_by)],
    ["Approval deadline", fmtDate(f.approval_deadline)],
    ["Cost the sender stated", fmtInr(f.additional_cost_stated)],
  ];

  return (
    <>
      {risk ? (
        <Panel
          title="Risk, scored twice"
          id="risk"
          aside={risk.level ? <Chip tone={RISK_TONE[risk.level] ?? "neutral"}>Used: {risk.level}</Chip> : null}
        >
          <p className="muted">A rule scorer and the AI score risk separately. If they differ, the higher one is used until a person decides.</p>
          {disagree ? (
            <Notice tone="person" title="The two readings disagree">
              The case is held for a person. Both readings are shown below so you can judge for yourself.
            </Notice>
          ) : null}
          <div className="compare">
            <Reading
              title="Rule scorer"
              source="Fixed rules on shortfall, delay and dates"
              level={risk.rule?.level}
              impact={risk.rule?.impact}
              urgency={risk.rule?.urgency}
              reasons={risk.rule?.why ?? []}
            />
            <Reading
              title="AI reading"
              source="Language model, checked against the facts"
              level={risk.llm?.risk}
              impact={risk.llm?.impact}
              urgency={risk.llm?.urgency}
              reasons={risk.llm?.reasons ?? []}
            />
          </div>
        </Panel>
      ) : null}

      <Panel title="Key numbers" id="numbers">
        <dl className="stats">
          {facts.map(([label, v]) => (
            <Stat key={label} label={label} value={v} />
          ))}
        </dl>
      </Panel>

      {f.inventory && f.inventory.length > 0 ? (
        <Panel title="Stock the system reported" id="inventory">
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Inventory snapshots</caption>
              <thead>
                <tr>
                  <th scope="col">Location</th>
                  <th scope="col">On hand</th>
                  <th scope="col">Snapshot age</th>
                </tr>
              </thead>
              <tbody>
                {f.inventory.map((i) => (
                  <tr key={`${i.location}-${i.age_s}`}>
                    <th scope="row">{i.location}</th>
                    <td className="num">{i.on_hand}</td>
                    <td className="num">{ageText(i.age_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}

      <Panel title="How the AI read the message" id="intake">
        <dl className="kv">
          <div>
            <dt>Reading mode</dt>
            <dd>{f.intake?.mode ? humanize(f.intake.mode) : "none"}</dd>
          </div>
          <div>
            <dt>Issues found</dt>
            <dd className="num">{f.intake?.issues_found ?? "none"}</dd>
          </div>
          <div>
            <dt>Inputs used</dt>
            <dd className="num">{f.inputs_used !== undefined && f.inputs_total !== undefined ? `${f.inputs_used} of ${f.inputs_total}` : "none"}</dd>
          </div>
          <div>
            <dt>Cancellation</dt>
            <dd>{f.intake?.is_cancellation ? "Yes" : "No"}</dd>
          </div>
        </dl>
        {f.intake?.degraded ? (
          <Notice tone="person" title="Degraded reading">
            The AI model was unavailable, so a rule-based reader did the work. Values it could not prove were left empty.
          </Notice>
        ) : null}
        {f.data_notes && f.data_notes.length > 0 ? (
          <>
            <h3>Data notes</h3>
            <ul>
              {f.data_notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          </>
        ) : null}
      </Panel>
    </>
  );
}
