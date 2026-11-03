import { Link, useSearchParams } from "react-router";
import { useMailbox, useMarkRead } from "../api/hooks";
import type { MailboxMessage } from "../api/types";
import { useUser } from "../components/Auth";
import { ActionError, Boundary, Chip, Empty, PageHeader, Skeleton } from "../components/ui";
import { fmtDateTime, humanize, plural } from "../lib/format";

export function MailboxPage() {
  const user = useUser();
  const [params, setParams] = useSearchParams();
  const isAdmin = user.role === "admin";
  const scope = isAdmin && params.get("scope") === "all" ? "all" : "mine";
  const unreadOnly = params.get("unread") === "1";
  const box = useMailbox(scope, unreadOnly);
  const mark = useMarkRead();

  function setParam(key: string, value: string | null) {
    setParams(
      (prev) => {
        const n = new URLSearchParams(prev);
        if (value) n.set(key, value);
        else n.delete(key);
        return n;
      },
      { replace: true },
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Mailbox"
        lead="The sandbox mail system. Every message the platform sends lands here, so you can read exactly what a supplier or approver would receive. Nothing leaves for the real internet."
      />
      <div className="toolbar">
        <div className="segmented" role="group" aria-label="Show">
          <button type="button" className={`segmented__btn${!unreadOnly ? " segmented__btn--on" : ""}`} aria-pressed={!unreadOnly} onClick={() => setParam("unread", null)}>
            All messages
          </button>
          <button type="button" className={`segmented__btn${unreadOnly ? " segmented__btn--on" : ""}`} aria-pressed={unreadOnly} onClick={() => setParam("unread", "1")}>
            Unread {box.data ? <span className="num">{box.data.unread}</span> : null}
          </button>
        </div>
        {isAdmin ? (
          <div className="segmented" role="group" aria-label="Whose mailbox">
            <button type="button" className={`segmented__btn${scope === "mine" ? " segmented__btn--on" : ""}`} aria-pressed={scope === "mine"} onClick={() => setParam("scope", null)}>
              Mine
            </button>
            <button type="button" className={`segmented__btn${scope === "all" ? " segmented__btn--on" : ""}`} aria-pressed={scope === "all"} onClick={() => setParam("scope", "all")}>
              Everyone
            </button>
          </div>
        ) : null}
      </div>

      <Boundary
        query={box}
        what="the mailbox"
        skeleton={<Skeleton rows={5} label="Loading the mailbox" />}
        isEmpty={(d) => d.messages.length === 0}
        empty={
          <Empty
            title={unreadOnly ? "No unread messages" : "The mailbox is empty"}
            action={
              unreadOnly ? (
                <button type="button" className="btn" onClick={() => setParam("unread", null)}>
                  Show all messages
                </button>
              ) : null
            }
          >
            {unreadOnly ? "You have read everything." : "Messages appear here when the platform sends reminders, questions or supplier updates."}
          </Empty>
        }
      >
        {(d) => (
          <>
            <p className="muted">{plural(d.messages.length, "message")} shown.</p>
            <ul className="messages messages--boxed">
              {d.messages.map((m) => (
                <Row key={m.id} m={m} onRead={() => mark.mutate(m.id)} reading={mark.isPending} />
              ))}
            </ul>
            <ActionError error={mark.error} what="mark the message read" />
          </>
        )}
      </Boundary>
    </div>
  );
}

function Row({ m, onRead, reading }: { m: MailboxMessage; onRead: () => void; reading: boolean }) {
  return (
    <li>
      <p className="message__head">
        <strong>{m.subject}</strong> {m.read_at ? <Chip>Read</Chip> : <Chip tone="person">Unread</Chip>} <Chip>{humanize(m.kind)}</Chip>
      </p>
      <p className="muted">
        To {m.recipient}, <span className="num">{fmtDateTime(m.delivered_at)}</span>.{" "}
        {m.case_id ? (
          <Link to={`/cases/${m.case_id}`} className="rowlink num">
            {m.case_id}
          </Link>
        ) : null}
      </p>
      <details className="details" onToggle={(e) => { if ((e.currentTarget as HTMLDetailsElement).open && !m.read_at) onRead(); }}>
        <summary>Read the message</summary>
        <pre className="source">{m.body}</pre>
      </details>
      {!m.read_at ? (
        <button type="button" className="btn btn--quiet" disabled={reading} onClick={onRead}>
          Mark read
        </button>
      ) : null}
    </li>
  );
}
