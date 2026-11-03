import { useClosure, useClosureAction, useReopen, useTickItem } from "../../api/hooks";
import type { Closure, ClosureItem } from "../../api/types";
import { ActionError, Boundary, Chip, Empty, Notice, Panel, Skeleton } from "../../components/ui";
import { Confirm } from "../../components/Confirm";
import { fmtDateTime, humanize } from "../../lib/format";
import { canRunClosure } from "../../lib/permissions";
import type { CaseTabProps } from "./shared";

const LIVE = new Set(["APPROVED", "EXECUTING", "MONITORING"]);

export function ClosureTab({ detail, user }: CaseTabProps) {
  const id = detail.case.id;
  const closure = useClosure(id);
  const allowed = canRunClosure(user, detail.case);

  return (
    <Boundary query={closure} what="closure" skeleton={<Skeleton rows={5} label="Loading closure" />}>
      {(c) => (
        <>
          <Controls caseId={id} closure={c} allowed={allowed} />
          <Checklist caseId={id} closure={c} allowed={allowed} isFinance={user.role === "finance_director"} />
          <SummaryPanel caseId={id} closure={c} allowed={allowed} />
        </>
      )}
    </Boundary>
  );
}

/** The chip beside "Closing the case". Only a live case can be "ready"; before approval or after closing the word would mislead. */
function readiness(c: Closure) {
  if (c.status === "CLOSED") return <Chip tone="ok">Closed</Chip>;
  if (!LIVE.has(c.status)) return null;
  return <Chip tone={c.can_close ? "ok" : "person"}>{c.can_close ? "Ready to close" : "Not ready to close"}</Chip>;
}

function Controls({ caseId, closure, allowed }: { caseId: string; closure: Closure; allowed: boolean }) {
  const act = useClosureAction(caseId);
  const reopen = useReopen(caseId);
  const status = closure.status;
  const labelOf = (key: string) => closure.items.find((i) => i.key === key)?.label ?? humanize(key);

  return (
    <Panel title="Closing the case" id="controls" aside={readiness(closure)}>
      <p className="muted">Closing is always a person's decision. The AI can write the summary but cannot close.</p>
      {closure.reopen_count > 0 ? <p>Reopened <span className="num">{closure.reopen_count}</span> time(s). The checklist restarted each time.</p> : null}

      {!allowed && LIVE.has(status) ? (
        <Notice tone="neutral" title="Approvers run closure">Only a manager, regional head or finance director of this region can start, tick or close. You can read the checklist.</Notice>
      ) : null}

      {status === "CLOSED" ? (
        <>
          <p>
            Closed <span className="num">{fmtDateTime(closure.closed_at)}</span>.
          </p>
          {allowed ? (
            <Confirm
              label="Reopen"
              prompt="Reopen this case? The closure checklist starts again."
              confirmLabel="Yes, reopen"
              reasonLabel="Why reopen it? (needed, at least 3 characters)"
              minReason={3}
              pending={reopen.isPending}
              onConfirm={(reason) => reopen.mutate({ reason })}
            />
          ) : null}
        </>
      ) : null}

      {LIVE.has(status) && allowed ? (
        <div className="chips">
          {status === "APPROVED" ? (
            <Confirm label="Start execution" tone="primary" prompt="Mark the plan as being carried out?" confirmLabel="Yes, start" pending={act.isPending} onConfirm={() => act.mutate("start")} />
          ) : null}
          {status === "EXECUTING" ? (
            <Confirm label="Start monitoring" tone="primary" prompt="The plan is done. Watch for the outcome?" confirmLabel="Yes, start monitoring" pending={act.isPending} onConfirm={() => act.mutate("monitor")} />
          ) : null}
          <Confirm
            label="Close the case"
            tone={closure.can_close ? "primary" : "plain"}
            prompt="Close this case? A summary is written from the audit trail."
            confirmLabel="Yes, close"
            disabled={!closure.can_close}
            pending={act.isPending}
            onConfirm={() => act.mutate("close")}
          />
        </div>
      ) : null}

      {LIVE.has(status) && !closure.can_close && closure.missing.length > 0 ? (
        <>
          <p>Still needed before closing:</p>
          <ul>
            {closure.missing.map((k) => (
              <li key={k}>{labelOf(k)}</li>
            ))}
          </ul>
        </>
      ) : null}

      {!LIVE.has(status) && status !== "CLOSED" ? (
        <p className="muted">Closure opens once the plan is approved. The case is {humanize(status).toLowerCase()} now.</p>
      ) : null}
      <ActionError error={act.error ?? reopen.error} what="update closure" />
    </Panel>
  );
}

function Checklist({ caseId, closure, allowed, isFinance }: { caseId: string; closure: Closure; allowed: boolean; isFinance: boolean }) {
  const tick = useTickItem(caseId);
  const live = LIVE.has(closure.status);

  if (closure.items.length === 0) {
    return (
      <Panel title="Closure checklist" id="checklist">
        <Empty title="No checklist yet">It is created when the plan is approved.</Empty>
      </Panel>
    );
  }

  return (
    <Panel title="Closure checklist" id="checklist">
      <p className="muted">Some items tick themselves from the audit trail. Manual items need a named person.</p>
      <div className="table-wrap">
        <table className="table table--stack">
          <caption className="visually-hidden">Closure checklist</caption>
          <thead>
            <tr>
              <th scope="col">Item</th>
              <th scope="col">Type</th>
              <th scope="col">State</th>
              <th scope="col">Done by</th>
              <th scope="col">
                <span className="visually-hidden">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {closure.items.map((i) => (
              <ItemRow key={i.key} i={i} live={live} allowed={allowed} isFinance={isFinance} pending={tick.isPending} onTick={(note) => tick.mutate({ key: i.key, note: note || undefined })} />
            ))}
          </tbody>
        </table>
      </div>
      <ActionError error={tick.error} what="tick the item" />
    </Panel>
  );
}

function ItemRow({ i, live, allowed, isFinance, pending, onTick }: { i: ClosureItem; live: boolean; allowed: boolean; isFinance: boolean; pending: boolean; onTick: (note: string) => void }) {
  const financeOnly = i.key === "spend_recorded";
  return (
    <tr>
      <th scope="row">
        {i.label} {i.required ? null : <span className="table__sub">Optional</span>}
        <span className="table__sub">{i.detail}</span>
      </th>
      <td data-label="Type">{i.kind === "derived" ? <Chip tone="instrument">From the audit trail</Chip> : <Chip tone="person">By a person</Chip>}</td>
      <td data-label="State">
        <Chip tone={i.done ? "ok" : "person"}>{i.done ? "Done" : "Open"}</Chip>
      </td>
      <td data-label="Done by">
        {i.by ? (i.by_name ?? i.by) : <span className="muted">{i.done ? "Checked from the audit trail" : "nobody yet"}</span>}
        {i.at ? <span className="table__sub num">{fmtDateTime(i.at)}</span> : null}
        {i.note ? <span className="table__sub">{i.note}</span> : null}
      </td>
      <td className="table__act">
        {i.kind === "manual" && !i.done && live && allowed ? (
          financeOnly && !isFinance ? (
            <span className="muted">Finance director only</span>
          ) : (
            <Confirm
              label="Mark done"
              prompt={`Confirm that "${i.label}" is done? This is recorded under your name.`}
              confirmLabel="Yes, it is done"
              reasonLabel="Note (optional)"
              minReason={0}
              pending={pending}
              onConfirm={onTick}
            />
          )
        ) : null}
      </td>
    </tr>
  );
}

function SummaryPanel({ caseId, closure, allowed }: { caseId: string; closure: Closure; allowed: boolean }) {
  const act = useClosureAction(caseId);
  const s = closure.latest_summary;
  const canWrite = allowed && (LIVE.has(closure.status) || closure.status === "CLOSED");

  return (
    <Panel
      title="Case summary"
      id="summary"
      aside={s ? <Chip tone={s.source === "llm" ? "instrument" : "neutral"} title={s.source === "llm" ? "Written by the AI and checked against audit facts" : "Written from a fixed template"}>{s.source === "llm" ? "AI wrote this" : "Template wrote this"}</Chip> : null}
    >
      {s ? (
        <>
          <p className="summary__headline">{s.content.headline}</p>
          <p>{s.content.what_happened}</p>
          {s.content.decisions.length > 0 ? (
            <>
              <h3>Decisions</h3>
              <ul>
                {s.content.decisions.map((d) => (
                  <li key={d}>{d}</li>
                ))}
              </ul>
            </>
          ) : null}
          {s.content.open_items.length > 0 ? (
            <>
              <h3>Still open</h3>
              <ul>
                {s.content.open_items.map((d) => (
                  <li key={d}>{d}</li>
                ))}
              </ul>
            </>
          ) : null}
          <p className="muted">
            Written <span className="num">{fmtDateTime(s.created_at)}</span>. The AI may only cite facts from this case; code rejects numbers it cannot find.
          </p>
        </>
      ) : (
        <Empty title="No summary yet">A summary is written when the case closes, or when an approver asks for one.</Empty>
      )}
      {canWrite ? (
        <button type="button" className="btn" disabled={act.isPending} onClick={() => act.mutate("summary")}>
          {act.isPending ? "Writing" : s ? "Write it again" : "Write the summary"}
        </button>
      ) : null}
      <ActionError error={act.error} what="write the summary" />
    </Panel>
  );
}
