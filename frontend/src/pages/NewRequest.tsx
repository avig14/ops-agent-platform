import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { ApiError } from "../api/client";
import { useCreateCase, useScenarios } from "../api/hooks";
import type { Channel, Scenario } from "../api/types";
import { ActionError, Boundary, Chip, Empty, PageHeader, Panel, Skeleton } from "../components/ui";
import { humanize } from "../lib/format";

const MAX_BODY = 20000;
const CHANNELS: { value: Channel; label: string }[] = [
  { value: "email", label: "Email" },
  { value: "manual", label: "Manual entry" },
  { value: "supplier_update", label: "Supplier update" },
];

export function NewRequestPage() {
  const [params, setParams] = useSearchParams();
  const scenarios = useScenarios();
  const selectedId = params.get("scenario");
  const selected = scenarios.data?.find((s) => s.id === selectedId) ?? null;

  function pick(id: string | null) {
    setParams(id ? { scenario: id } : {}, { replace: true });
  }

  return (
    <div className="page">
      <PageHeader
        title="New request"
        lead="Paste an email or type a request. The intake agent extracts the facts; nothing is approved or sent without a person."
      />
      <div className="split">
        <div className="split__main">
          <RequestForm key={selected?.id ?? "blank"} scenario={selected} />
        </div>
        <aside className="split__side" aria-label="Scenario library">
          <Panel title="Scenario library" id="library">
            <p className="muted">One click loads a ready-made request for each edge case. Then submit it and follow the steps.</p>
            <Boundary
              query={scenarios}
              what="scenarios"
              skeleton={<Skeleton rows={6} label="Loading scenarios" />}
              isEmpty={(s) => s.length === 0}
              empty={<Empty title="No scenarios">The server returned an empty library.</Empty>}
            >
              {(list) => (
                <ul className="scenario-list">
                  {list.map((s) => (
                    <li key={s.id}>
                      <button
                        type="button"
                        className={`scenario${s.id === selectedId ? " scenario--on" : ""}`}
                        aria-pressed={s.id === selectedId}
                        onClick={() => pick(s.id === selectedId ? null : s.id)}
                      >
                        <span className="scenario__title">{s.title}</span>
                        <span className="scenario__meta">{s.edge_case ? humanize(s.edge_case) : "End-to-end sample"}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Boundary>
          </Panel>
        </aside>
      </div>
    </div>
  );
}

function RequestForm({ scenario }: { scenario: Scenario | null }) {
  const navigate = useNavigate();
  const create = useCreateCase();
  const [channel, setChannel] = useState<Channel>(scenario?.channel ?? "email");
  const [sender, setSender] = useState("");
  const [subject, setSubject] = useState(scenario?.subject ?? "");
  const [body, setBody] = useState(scenario?.body ?? "");
  const bodyOk = body.trim().length > 0 && body.length <= MAX_BODY;

  function submit(ev: FormEvent) {
    ev.preventDefault();
    if (!bodyOk) return;
    create.mutate(
      { channel, sender: sender.trim() || undefined, subject: subject.trim() || undefined, body },
      { onSuccess: (r) => navigate(`/cases/${r.case_id}`) },
    );
  }

  const bodyIssue = create.error instanceof ApiError ? create.error.issues.find((i) => i.loc.includes("body")) : undefined;

  return (
    <>
      {scenario ? (
        <Panel title={scenario.title} id="scenario">
          <p>{scenario.description}</p>
          <h3>Steps</h3>
          <ol className="steps">
            {scenario.setup.map((s) => (
              <li key={`setup-${s}`}>
                <Chip>Before you start</Chip> {s}
              </li>
            ))}
            {scenario.steps.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
          <h3>What the platform should do</h3>
          <p>{scenario.expected}</p>
        </Panel>
      ) : null}

      <form className="panel form" onSubmit={submit} aria-label="New request">
        <div className="form__row">
          <div className="field">
            <label htmlFor="channel">Channel</label>
            <select id="channel" value={channel} onChange={(e) => setChannel(e.target.value as Channel)}>
              {CHANNELS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="sender">From (optional)</label>
            <input id="sender" type="text" value={sender} maxLength={200} placeholder="name@company.com" onChange={(e) => setSender(e.target.value)} />
          </div>
        </div>
        <div className="field">
          <label htmlFor="subject">Subject (optional)</label>
          <input id="subject" type="text" value={subject} maxLength={300} onChange={(e) => setSubject(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="body">Message</label>
          <textarea
            id="body"
            rows={12}
            required
            value={body}
            aria-describedby="body-count"
            aria-invalid={!bodyOk && body.length > 0}
            onChange={(e) => setBody(e.target.value)}
          />
          <p id="body-count" className={`field__hint${body.length > MAX_BODY ? " text-bad" : ""}`}>
            <span className="num">{body.length}</span> of <span className="num">{MAX_BODY}</span> characters
          </p>
          {bodyIssue ? <p className="field__error">{bodyIssue.msg}</p> : null}
        </div>
        <div className="form__actions">
          <button type="submit" className="btn btn--primary" disabled={!bodyOk || create.isPending}>
            {create.isPending ? "Submitting" : "Submit request"}
          </button>
          <span className="muted">The AI reads it in a few seconds. You land on the case while it works.</span>
        </div>
        <ActionError error={create.error} what="submit the request" />
      </form>
    </>
  );
}
