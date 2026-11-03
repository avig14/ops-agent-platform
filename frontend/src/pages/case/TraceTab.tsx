import { Chip, Empty, Panel } from "../../components/ui";
import { summarisePayload } from "../../lib/case";
import { fmtDateTime, humanize } from "../../lib/format";
import type { CaseTabProps } from "./shared";

export function TraceTab({ detail }: CaseTabProps) {
  const msgs = detail.agent_messages;
  return (
    <>
      <Panel title="How the agents share what they know" id="shared-context">
        <p>
          Agents do not talk to each other freely. Each one reads only the inputs it needs from one shared case record and writes a typed
          result back. The orchestrator is plain code, not an AI: it moves the case from step to step and decides who runs next. Every hand-over
          below is stored, so you can see who told whom what.
        </p>
        <p className="muted">
          The raw message is untrusted. Only the intake agent sees it. Everything after that receives checked fields, not free text.
        </p>
      </Panel>
      <Panel title="Hand-overs between agents" id="trace" aside={<Chip tone="instrument">{msgs.length} recorded</Chip>}>
        {msgs.length === 0 ? (
          <Empty title="No hand-overs yet">They appear as the agents work.</Empty>
        ) : (
          <ol className="trace">
            {msgs.map((m) => (
              <li key={m.id}>
                <p className="trace__head">
                  <Chip tone="instrument">{humanize(m.from_agent)}</Chip>
                  <span aria-hidden="true"> to </span>
                  <span className="visually-hidden"> told </span>
                  <Chip tone={m.to_agent ? "instrument" : "neutral"}>{m.to_agent ? humanize(m.to_agent) : "Everyone"}</Chip>{" "}
                  <Chip>{humanize(m.kind)}</Chip> <span className="muted num">{fmtDateTime(m.created_at)}</span>
                </p>
                <p className="trace__sum">{summarisePayload(m.payload)}</p>
                <details className="details">
                  <summary>Show the full payload</summary>
                  <pre className="source">{JSON.stringify(m.payload, null, 2)}</pre>
                </details>
              </li>
            ))}
          </ol>
        )}
      </Panel>
    </>
  );
}
