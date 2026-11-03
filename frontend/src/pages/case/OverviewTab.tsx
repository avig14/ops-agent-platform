import { useState, type FormEvent } from "react";
import { useEditFields, useMessage, useScenarios } from "../../api/hooks";
import type { Channel, FieldRow, FieldState, SourceMessage } from "../../api/types";
import { ActionError, Chip, Empty, Panel } from "../../components/ui";
import { Confirm } from "../../components/Confirm";
import { signalList } from "../../lib/case";
import { fmtDateTime, fmtPct, humanize } from "../../lib/format";
import { fieldLabel, FINISHED, type Tone } from "../../lib/labels";
import { canChangeCase } from "../../lib/permissions";
import { splitByQuotes } from "../../lib/quotes";
import type { CaseTabProps } from "./shared";

const FIELD_STATE: Record<FieldState, { label: string; tone: Tone; title: string }> = {
  ai: { label: "AI read", tone: "instrument", title: "Read by the AI and checked by code" },
  missing: { label: "Missing", tone: "person", title: "Not in the message, or the AI could not prove it" },
  confirmed: { label: "Confirmed", tone: "ok", title: "A person confirmed this value" },
  corrected: { label: "Corrected", tone: "person", title: "A person replaced the AI's value" },
};

export function OverviewTab({ detail, user }: CaseTabProps) {
  const c = detail.case;
  const canEdit = canChangeCase(user, c) && !FINISHED.has(c.status);
  return (
    <>
      <FactsPanel detail={detail} canEdit={canEdit} />
      <SourcesPanel sources={detail.sources} fields={detail.fields} />
      {canEdit ? <NewMessagePanel caseId={c.id} /> : null}
    </>
  );
}

function FactsPanel({ detail, canEdit }: { detail: CaseTabProps["detail"]; canEdit: boolean }) {
  const edit = useEditFields(detail.case.id);
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState("");

  function start(f: FieldRow) {
    setEditing(f.name);
    setValue(f.value ?? "");
  }

  function save(ev: FormEvent) {
    ev.preventDefault();
    if (!editing || value.trim().length === 0) return;
    edit.mutate({ fields: { [editing]: value.trim() }, reason: "Corrected on the case page" }, { onSuccess: () => setEditing(null) });
  }

  return (
    <Panel title="What the AI understood" id="facts" aside={<span className="muted">Every value must be quoted from the message.</span>}>
      {detail.fields.length === 0 ? (
        <Empty title="No facts yet">The intake agent has not finished reading the message.</Empty>
      ) : (
        <>
          <form id="edit-field" onSubmit={save} />
          <div className="table-wrap">
            <table className="table table--stack">
              <caption className="visually-hidden">Facts extracted from the message</caption>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">Value</th>
                  <th scope="col">Quoted from the message</th>
                  <th scope="col">Confidence</th>
                  <th scope="col">State</th>
                  {canEdit ? (
                    <th scope="col">
                      <span className="visually-hidden">Edit</span>
                    </th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {detail.fields.map((f) => {
                  const st = FIELD_STATE[f.state];
                  return (
                    <tr key={f.name}>
                      <th scope="row">{fieldLabel(f.name)}</th>
                      <td data-label="Value">
                        {editing === f.name ? (
                          <input
                            form="edit-field"
                            aria-label={`New value for ${fieldLabel(f.name)}`}
                            value={value}
                            onChange={(e) => setValue(e.target.value)}
                            maxLength={200}
                            autoFocus
                          />
                        ) : f.value ? (
                          <span className="num">{f.value}</span>
                        ) : (
                          <span className="muted">none</span>
                        )}
                      </td>
                      <td data-label="Quoted from the message">{f.quote ? <q className="quote">{f.quote}</q> : <span className="muted">no quote</span>}</td>
                      <td data-label="Confidence">
                        <Chip tone="instrument" title={signalList(f.signals).map((s) => `${s.label}: ${s.value}`).join(". ")}>
                          <span className="num">{fmtPct(f.confidence)}</span>
                        </Chip>
                        <span className="table__sub">
                          {signalList(f.signals)
                            .map((s) => `${s.label}: ${s.value}`)
                            .join(", ")}
                        </span>
                      </td>
                      <td data-label="State">
                        <Chip tone={st.tone} title={st.title}>
                          {st.label}
                        </Chip>
                      </td>
                      {canEdit ? (
                        <td className="table__act">
                          {editing === f.name ? (
                            <span className="chips">
                              <button type="submit" form="edit-field" className="btn btn--primary" disabled={edit.isPending || value.trim().length === 0}>
                                Save
                              </button>
                              <button type="button" className="btn" onClick={() => setEditing(null)}>
                                Cancel
                              </button>
                            </span>
                          ) : (
                            <button type="button" className="btn" onClick={() => start(f)} disabled={edit.isPending}>
                              Edit
                            </button>
                          )}
                        </td>
                      ) : null}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <ActionError error={edit.error} what="save the correction" />
          {canEdit ? (
            <p className="muted">A correction replaces the AI value, re-checks the plan and withdraws any approval that depended on the old value.</p>
          ) : null}
        </>
      )}
    </Panel>
  );
}

function SourcesPanel({ sources, fields }: { sources: SourceMessage[]; fields: FieldRow[] }) {
  const quotes = fields.map((f) => f.quote);
  return (
    <Panel title="Original messages" id="sources" aside={<span className="muted">Highlighted text is what the AI quoted.</span>}>
      {sources.length === 0 ? (
        <Empty title="No message to show">You may not have access to the raw message, or none was stored.</Empty>
      ) : (
        <ul className="sources">
          {sources.map((s) => (
            <li key={`${s.role}-${s.id}`}>
              <p className="source__meta">
                <Chip>{humanize(s.channel)}</Chip> <Chip>{humanize(s.role)}</Chip>{" "}
                <span className="muted">
                  {s.sender ?? "unknown sender"}, <span className="num">{fmtDateTime(s.received_at)}</span>
                </span>
              </p>
              {s.subject ? <p className="source__subject">{s.subject}</p> : null}
              <pre className="source">
                {splitByQuotes(s.body, quotes).map((seg, i) => (seg.hit ? <mark key={i}>{seg.text}</mark> : <span key={i}>{seg.text}</span>))}
              </pre>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

type Kind = "reply" | "revision" | "cancel";
const KINDS: { value: Kind; label: string; help: string }[] = [
  { value: "reply", label: "Reply with information", help: "Answers a question the platform asked." },
  { value: "revision", label: "Revision from a supplier or requester", help: "The facts changed. The plan is rebuilt and approvals are withdrawn." },
  { value: "cancel", label: "Cancel the request", help: "Pending approvals are withdrawn and approvers are told." },
];
const SAMPLE_FOR: Record<Kind, string | null> = { reply: null, revision: "changing_circumstances", cancel: "cancellation_after_approval" };

function NewMessagePanel({ caseId }: { caseId: string }) {
  const send = useMessage(caseId);
  const scenarios = useScenarios();
  const [kind, setKind] = useState<Kind>("reply");
  const [channel, setChannel] = useState<Channel>("manual");
  const [body, setBody] = useState("");
  const sampleId = SAMPLE_FOR[kind];
  const sample = sampleId ? scenarios.data?.find((s) => s.id === sampleId) : undefined;
  const help = KINDS.find((k) => k.value === kind)?.help ?? "";
  const ok = body.trim().length > 0 && body.length <= 20000;

  function submit() {
    if (!ok) return;
    send.mutate({ kind, channel, body }, { onSuccess: () => setBody("") });
  }

  return (
    <Panel title="Add a message to this case" id="message">
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          if (kind !== "cancel") submit();
        }}
      >
        <div className="form__row">
          <div className="field">
            <label htmlFor="msg-kind">What is this?</label>
            <select id="msg-kind" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              {KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
            <p className="field__hint">{help}</p>
          </div>
          <div className="field">
            <label htmlFor="msg-channel">Channel</label>
            <select id="msg-channel" value={channel} onChange={(e) => setChannel(e.target.value as Channel)}>
              <option value="manual">Manual entry</option>
              <option value="email">Email</option>
              <option value="supplier_update">Supplier update</option>
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="msg-body">Message</label>
          <textarea id="msg-body" rows={5} value={body} onChange={(e) => setBody(e.target.value)} maxLength={20000} />
        </div>
        <div className="form__actions">
          {kind === "cancel" ? (
            <Confirm
              label="Cancel the request"
              tone="danger"
              prompt="Cancel this whole request? Pending approvals are withdrawn and unsent messages are stopped."
              confirmLabel="Yes, cancel it"
              disabled={!ok}
              pending={send.isPending}
              onConfirm={submit}
            />
          ) : (
            <button type="submit" className="btn btn--primary" disabled={!ok || send.isPending}>
              {send.isPending ? "Sending" : "Send to the case"}
            </button>
          )}
          {sample ? (
            <button type="button" className="btn btn--quiet" onClick={() => setBody(sample.body)}>
              Fill with the sample text
            </button>
          ) : null}
        </div>
        <ActionError error={send.error} what="add the message" />
      </form>
    </Panel>
  );
}
