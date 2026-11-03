import { useState } from "react";
import { useComms, useOutboxAction } from "../../api/hooks";
import type { OutMessage } from "../../api/types";
import { ActionError, Boundary, Chip, Empty, Panel, Skeleton } from "../../components/ui";
import { Confirm } from "../../components/Confirm";
import { fmtDateTime, humanize } from "../../lib/format";
import { MESSAGE } from "../../lib/labels";
import { isApproverRole } from "../../lib/permissions";
import type { CaseTabProps } from "./shared";

const RETRYABLE = new Set(["failed", "dead", "delivery_unknown"]);

export function CommsTab({ detail, user }: CaseTabProps) {
  const caseId = detail.case.id;
  const comms = useComms(caseId);
  const act = useOutboxAction(caseId);
  const canReview = isApproverRole(user.role);
  const canRetry = canReview || user.role === "admin";

  return (
    <Boundary
      query={comms}
      what="communications"
      skeleton={<Skeleton rows={4} label="Loading communications" />}
      isEmpty={(d) => d.messages.length === 0 && d.notices.length === 0}
      empty={
        <Panel>
          <Empty title="No messages yet">Drafts appear here when the case needs a question or a supplier message. Nothing leaves the platform until a person releases it.</Empty>
        </Panel>
      }
    >
      {(d) => (
        <>
          <Panel title="Messages" id="messages" aside={<span className="muted">AI drafts. A person reviews. Code sends.</span>}>
            {d.messages.length === 0 ? (
              <p className="muted">No outgoing messages.</p>
            ) : (
              <ul className="messages">
                {d.messages.map((m) => (
                  <MessageRow key={m.id} m={m} canReview={canReview} canRetry={canRetry} pending={act.isPending} onAct={(v) => act.mutate({ id: m.id, ...v })} />
                ))}
              </ul>
            )}
            <ActionError error={act.error} what="update the message" />
          </Panel>

          {d.notices.length > 0 ? (
            <Panel title="In-app notices" id="notices">
              <p className="muted">Short notices delivered inside the platform, for example reminders and escalations.</p>
              <ul className="messages">
                {d.notices.map((n) => (
                  <li key={n.id}>
                    <p>
                      <strong>{n.subject}</strong> <Chip>{humanize(n.kind)}</Chip>
                    </p>
                    <p className="muted">
                      To {n.recipient}, <span className="num">{fmtDateTime(n.delivered_at)}</span>
                    </p>
                    <p>{n.body}</p>
                  </li>
                ))}
              </ul>
            </Panel>
          ) : null}
        </>
      )}
    </Boundary>
  );
}

interface RowProps {
  m: OutMessage;
  canReview: boolean;
  canRetry: boolean;
  pending: boolean;
  onAct: (v: { action: "retry" | "release" | "discard"; subject?: string; body?: string; reason?: string }) => void;
}

function MessageRow({ m, canReview, canRetry, pending, onAct }: RowProps) {
  const st = MESSAGE[m.status];
  const [subject, setSubject] = useState(m.subject);
  const [body, setBody] = useState(m.body);
  const draft = m.status === "draft";
  const edited = subject !== m.subject || body !== m.body;

  return (
    <li>
      <p className="message__head">
        <strong>{m.subject}</strong> <Chip tone={st.tone}>{st.label}</Chip> {m.edited ? <Chip tone="person">Edited by a person</Chip> : null}
      </p>
      <p className="muted">
        To {m.recipient}
        {m.audience ? `, ${humanize(m.audience)}` : ""}. {humanize(m.kind)}.
        {m.source ? ` Written by ${m.source === "llm" ? "the AI" : "a template"}.` : ""}
        {m.attempts !== undefined ? (
          <>
            {" "}
            Attempts <span className="num">{m.attempts}</span>
            {m.max_attempts ? <> of <span className="num">{m.max_attempts}</span></> : null}.
          </>
        ) : null}
        {m.sent_at ? (
          <>
            {" "}
            Sent <span className="num">{fmtDateTime(m.sent_at)}</span>.
          </>
        ) : null}
      </p>
      {m.last_error ? (
        <p className="text-bad">
          Last error: {m.last_error}
          {m.reason_code ? <span className="muted num"> {m.reason_code}</span> : null}
        </p>
      ) : null}

      {draft && canReview ? (
        <div className="form">
          <div className="field">
            <label htmlFor={`s-${m.id}`}>Subject</label>
            <input id={`s-${m.id}`} value={subject} maxLength={200} onChange={(e) => setSubject(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor={`b-${m.id}`}>Message</label>
            <textarea id={`b-${m.id}`} rows={7} value={body} maxLength={5000} onChange={(e) => setBody(e.target.value)} />
          </div>
          <div className="form__actions">
            <Confirm
              label={edited ? "Release with my edits" : "Release as written"}
              tone="primary"
              prompt="Release this message? It is queued and sent by code. This is recorded under your name."
              confirmLabel="Yes, release"
              disabled={!subject.trim() || !body.trim()}
              pending={pending}
              onConfirm={() => onAct({ action: "release", subject: edited ? subject : undefined, body: edited ? body : undefined })}
            />
            <Confirm
              label="Discard"
              tone="danger"
              prompt="Discard this draft? It will not be sent."
              confirmLabel="Yes, discard"
              reasonLabel="Why discard it? (optional)"
              minReason={0}
              pending={pending}
              onConfirm={(reason) => onAct({ action: "discard", reason: reason || undefined })}
            />
          </div>
        </div>
      ) : (
        <details className="details">
          <summary>Show the message</summary>
          <pre className="source">{m.body}</pre>
        </details>
      )}

      {RETRYABLE.has(m.status) && canRetry ? (
        <div className="form__actions">
          <button type="button" className="btn" disabled={pending} onClick={() => onAct({ action: "retry" })}>
            Retry sending
          </button>
        </div>
      ) : null}
    </li>
  );
}
