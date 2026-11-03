import { useState } from "react";
import { useAdminRun, useClock, useLlmStatus, useOutboxStats, useSetChaos, useSetClock, useSetCommsChaos, useVerifyAudit } from "../api/hooks";
import { ActionError, Boundary, Chip, Notice, PageHeader, Panel, Skeleton, Stat } from "../components/ui";
import { fmtDateTime, fmtNumber, humanize, shortHash } from "../lib/format";
import type { Tone } from "../lib/labels";
import { EvalsPanel } from "./admin/EvalsPanel";

const LLM_CHAOS: { value: string | null; label: string }[] = [
  { value: null, label: "Off" },
  { value: "503", label: "Overloaded (503)" },
  { value: "429", label: "Rate limited (429)" },
  { value: "402", label: "Billing failure (402)" },
  { value: "401", label: "Bad key (401)" },
  { value: "timeout", label: "Timeout" },
  { value: "garbage", label: "Garbage output" },
];

const MAIL_CHAOS: { value: string | null; label: string }[] = [
  { value: null, label: "Off" },
  { value: "fail", label: "Fail then retry (503)" },
  { value: "bounce", label: "Bounce (permanent)" },
  { value: "timeout", label: "Sent, no acknowledgement" },
];

const CIRCUIT: Record<string, { label: string; tone: Tone }> = {
  closed: { label: "Closed, working", tone: "ok" },
  half_open: { label: "Half open, probing", tone: "person" },
  open: { label: "Open, skipped", tone: "bad" },
};

const DAY = 86_400;

export function AdminPage() {
  return (
    <div className="page">
      <PageHeader
        title="Admin"
        lead="Controls for the live demo. Each switch makes a real failure happen, so you can watch the platform detect it, tell the user, recover and record it."
      />
      <div className="split split--even">
        <LlmPanel />
        <MailPanel />
      </div>
      <div className="split split--even">
        <ClockPanel />
        <JobsPanel />
      </div>
      <AuditPanel />
      <EvalsPanel />
    </div>
  );
}

function ModeButtons({
  options,
  current,
  pending,
  onPick,
  label,
}: {
  options: { value: string | null; label: string }[];
  current: string | null | undefined;
  pending: boolean;
  onPick: (v: string | null) => void;
  label: string;
}) {
  return (
    <div className="segmented segmented--wrap" role="group" aria-label={label}>
      {options.map((o) => {
        const on = (current ?? null) === o.value;
        return (
          <button key={o.label} type="button" className={`segmented__btn${on ? " segmented__btn--on" : ""}`} aria-pressed={on} disabled={pending} onClick={() => onPick(o.value)}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function LlmPanel() {
  const status = useLlmStatus();
  const chaos = useSetChaos();
  return (
    <Panel title="AI model" id="llm" aside={status.data ? <Chip tone="instrument">{status.data.mode === "replay" ? "Replay mode" : "Live"}</Chip> : null}>
      <Boundary query={status} what="the AI status" skeleton={<Skeleton rows={3} label="Loading the AI status" />}>
        {(s) => (
          <>
            <dl className="stats">
              <Stat label="Calls today" value={fmtNumber(s.calls_today)} />
            </dl>
            <div className="table-wrap">
              <table className="table">
                <caption className="visually-hidden">Model fallback chain and circuit state</caption>
                <thead>
                  <tr>
                    <th scope="col">Model, first choice first</th>
                    <th scope="col">Circuit</th>
                    <th scope="col">Failures</th>
                  </tr>
                </thead>
                <tbody>
                  {s.models.map((m) => {
                    const c = CIRCUIT[m.circuit] ?? { label: humanize(m.circuit), tone: "neutral" as Tone };
                    return (
                      <tr key={m.model}>
                        <th scope="row" className="num">
                          {m.model}
                        </th>
                        <td>
                          <Chip tone={c.tone}>{c.label}</Chip>
                        </td>
                        <td className="num">{m.failures}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <h3>Make the AI fail</h3>
            <ModeButtons options={LLM_CHAOS} current={s.chaos} pending={chaos.isPending} onPick={(v) => chaos.mutate(v)} label="AI failure to inject" />
            <p className="muted">When every model fails the case turns Degraded. No spend is recommended and a person reviews it.</p>
          </>
        )}
      </Boundary>
      <ActionError error={chaos.error} what="change the failure mode" />
    </Panel>
  );
}

function MailPanel() {
  const stats = useOutboxStats();
  const chaos = useSetCommsChaos();
  return (
    <Panel title="Outgoing mail" id="mail" aside={stats.data?.fault.mode ? <Chip tone="bad">Fault on: {humanize(stats.data.fault.mode)}</Chip> : null}>
      <Boundary query={stats} what="the outbox" skeleton={<Skeleton rows={3} label="Loading the outbox" />}>
        {(s) => (
          <>
            <dl className="stats">
              {Object.entries(s.by_status).length === 0 ? <Stat label="Messages" value={0} /> : null}
              {Object.entries(s.by_status).map(([k, n]) => (
                <Stat key={k} label={humanize(k)} value={n} />
              ))}
              <Stat label="Oldest due" value={s.oldest_due_s === null ? "none" : `${Math.round(s.oldest_due_s)} s`} />
            </dl>
            <h3>Make the mail provider fail</h3>
            <ModeButtons options={MAIL_CHAOS} current={s.fault.mode} pending={chaos.isPending} onPick={(v) => chaos.mutate({ mode: v, count: v ? 3 : undefined })} label="Mail failure to inject" />
            <p className="muted">
              Applies to the next 3 sends. {s.fault.mode ? <span className="num">{s.fault.remaining} left.</span> : null} Failed sends retry, then go to the in-app notice, then the dead letter list.
            </p>
          </>
        )}
      </Boundary>
      <ActionError error={chaos.error} what="change the failure mode" />
    </Panel>
  );
}

function ClockPanel() {
  const clock = useClock();
  const set = useSetClock();
  return (
    <Panel title="Business clock" id="clock">
      <Boundary query={clock} what="the clock" skeleton={<Skeleton rows={2} label="Loading the clock" />}>
        {(c) => (
          <>
            <dl className="stats">
              <Stat label="Business time" value={fmtDateTime(c.now)} />
              <Stat label="Moved forward" value={`${Math.round(c.offset_seconds / 3600)} h`} />
            </dl>
            {c.enabled ? (
              <>
                <p className="muted">Jump time forward to show approval reminders and escalation. Only deadline logic moves. Audit timestamps stay real.</p>
                <div className="chips">
                  <button type="button" className="btn" disabled={set.isPending} onClick={() => set.mutate(c.offset_seconds + 12 * 3600)}>
                    Forward 12 hours
                  </button>
                  <button type="button" className="btn" disabled={set.isPending} onClick={() => set.mutate(c.offset_seconds + DAY)}>
                    Forward 1 day
                  </button>
                  <button type="button" className="btn btn--quiet" disabled={set.isPending || c.offset_seconds === 0} onClick={() => set.mutate(0)}>
                    Back to real time
                  </button>
                </div>
              </>
            ) : (
              <Notice title="Time travel is off here">The server was started without the demo clock switch.</Notice>
            )}
          </>
        )}
      </Boundary>
      <ActionError error={set.error} what="move the clock" />
    </Panel>
  );
}

function JobsPanel() {
  const run = useAdminRun();
  const [last, setLast] = useState<string | null>(null);

  function go(what: "sweep" | "reminders" | "outbox/run", label: string) {
    run.mutate(what, { onSuccess: () => setLast(label) });
  }

  return (
    <Panel title="Background jobs" id="jobs">
      <p className="muted">These run by themselves every few seconds. Run one now to see its effect straight away.</p>
      <div className="chips">
        <button type="button" className="btn" disabled={run.isPending} onClick={() => go("sweep", "Approval deadlines checked")}>
          Check approval deadlines
        </button>
        <button type="button" className="btn" disabled={run.isPending} onClick={() => go("reminders", "Reminders sent")}>
          Send missing-info reminders
        </button>
        <button type="button" className="btn" disabled={run.isPending} onClick={() => go("outbox/run", "Outbox processed")}>
          Send queued mail
        </button>
      </div>
      {last && !run.isPending && !run.isError ? (
        <p role="status" className="muted">
          {last}.
        </p>
      ) : null}
      <ActionError error={run.error} what="run the job" />
    </Panel>
  );
}

function AuditPanel() {
  const verify = useVerifyAudit();
  const bad = verify.data?.chains.filter((c) => !c.ok) ?? [];
  return (
    <Panel
      title="Audit tamper check"
      id="verify"
      aside={verify.data ? <Chip tone={verify.data.ok ? "ok" : "bad"}>{verify.data.ok ? "Every chain intact" : `${bad.length} chain broken`}</Chip> : null}
    >
      <p className="muted">Each entry holds a fingerprint of the one before it. Changing or deleting any entry breaks every fingerprint after it. This checks all cases at once.</p>
      <button type="button" className="btn" disabled={verify.isPending} onClick={() => verify.mutate()}>
        {verify.isPending ? "Checking" : verify.data ? "Check again" : "Check all chains"}
      </button>
      <ActionError error={verify.error} what="check the audit trail" />
      {verify.data ? (
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">Audit chain results</caption>
            <thead>
              <tr>
                <th scope="col">Chain</th>
                <th scope="col">Result</th>
                <th scope="col">Entries</th>
                <th scope="col">Latest fingerprint</th>
              </tr>
            </thead>
            <tbody>
              {verify.data.chains.map((c) => (
                <tr key={c.chain_key}>
                  <th scope="row" className="num">
                    {c.chain_key}
                  </th>
                  <td>
                    <Chip tone={c.ok ? "ok" : "bad"}>{c.ok ? "Intact" : "Broken"}</Chip>
                    {c.problem ? <span className="table__sub">{c.problem}</span> : null}
                  </td>
                  <td className="num">{c.checked}</td>
                  <td className="num" title={c.head_hash ?? undefined}>
                    {c.head_hash ? shortHash(c.head_hash) : "none"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Panel>
  );
}
