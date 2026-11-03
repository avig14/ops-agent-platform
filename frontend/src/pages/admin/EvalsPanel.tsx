import type { ReactNode } from "react";
import { useEvals } from "../../api/hooks";
import type { EvalBin, EvalMode, EvalRate, EvalSnapshot } from "../../api/types";
import { Boundary, Chip, Notice, Panel, Skeleton, Stat } from "../../components/ui";
import { bestMode, clamp01, countText, goldCount, injectionRows, MODE_LABEL, pct, rangeText, rateText, staleText, weakPayloads } from "../../lib/evals";
import { fmtDateTime, humanize, plural } from "../../lib/format";
import { fieldLabel } from "../../lib/labels";

/** A number with its uncertainty drawn: the bar is the likely range, the tick is the measured value. */
function Interval({ r }: { r: EvalRate }) {
  if (r.rate === null) return <span className="muted">none</span>;
  const [lo, hi] = r.ci95;
  return (
    <span className="interval" role="img" aria-label={`${pct(r.rate)}, likely ${rangeText(r)}`}>
      <span className="interval__range" style={{ left: `${clamp01(lo) * 100}%`, width: `${Math.max(0.02, clamp01(hi) - clamp01(lo)) * 100}%` }} />
      <span className="interval__tick" style={{ left: `${clamp01(r.rate) * 100}%` }} />
    </span>
  );
}

function RateCell({ r }: { r: EvalRate | null | undefined }) {
  return (
    <td className="num">
      {rateText(r)}
      <span className="table__sub">{r ? `${countText(r)}, likely ${rangeText(r)}` : ""}</span>
    </td>
  );
}

function Section({ title, lead, children }: { title: string; lead?: ReactNode; children: ReactNode }) {
  return (
    <section className="evals__section">
      <h3>{title}</h3>
      {lead ? <p className="muted">{lead}</p> : null}
      {children}
    </section>
  );
}

function Headline({ s }: { s: EvalSnapshot }) {
  const inj = s.scripted.injection;
  const fp = s.scripted.false_positives.false_positives;
  const gates = s.scripted.gates;
  return (
    <dl className="stats">
      <Stat label="Approval rules" value={`${s.scripted.policy.passed} of ${s.scripted.policy.checks}`} tone={s.scripted.policy.failed.length ? "bad" : "ok"} note="who may approve what" />
      <Stat label="AI output checks" value={`${gates.passed} of ${gates.of}`} tone={gates.passed === gates.of ? "ok" : "bad"} note={`${plural(gates.limitations.length, "known gap")}, see below`} />
      <Stat label="False alarms on normal emails" value={countText(fp)} tone={fp.k ? "bad" : "ok"} note={`likely ${rangeText(fp)}`} />
      <Stat label="Unseen attacks caught" value={rateText(inj.holdout.detected)} tone="person" note={`${countText(inj.holdout.detected)}; other layers back it up`} />
    </dl>
  );
}

function ExtractionSection({ s }: { s: EvalSnapshot }) {
  const modes = s.real_model.extraction.modes;
  const best = bestMode(modes);
  const order = ["rules_only", "llm", "llm+second"].flatMap((id) => {
    const v = modes[id];
    return v ? [{ id, v }] : [];
  });
  const m: EvalMode | null = (best ? modes[best] : undefined) ?? null;
  return (
    <Section
      title="Does it read the email correctly?"
      lead={`${s.real_model.extraction.cases} hand-labelled emails, 9 fields each. Precision: of the values it filled in, how many were right. Recall: of the values the email states, how many it found.`}
    >
      <div className="table-wrap">
        <table className="table">
          <caption className="visually-hidden">Extraction accuracy by method</caption>
          <thead>
            <tr>
              <th scope="col">Method</th>
              <th scope="col">Emails scored</th>
              <th scope="col">Precision</th>
              <th scope="col">Recall</th>
              <th scope="col">Invented values</th>
              <th scope="col">Wrong values</th>
            </tr>
          </thead>
          <tbody>
            {order.map(({ id, v }) => {
              return (
                <tr key={id}>
                  <th scope="row">{MODE_LABEL[id] ?? humanize(id)}</th>
                  <td className="num">
                    {v.scored}
                    {v.not_scored.length ? <span className="table__sub">{v.not_scored.length} not scored</span> : null}
                  </td>
                  {v.scored === 0 ? (
                    <td colSpan={4} className="muted">
                      Not measured: no recorded AI answers for this method.
                    </td>
                  ) : (
                    <>
                      <td className="num">
                        {pct(v.overall.precision)}
                        <span className="table__sub">
                          likely between {pct(v.precision_ci95[0])} and {pct(v.precision_ci95[1])}
                        </span>
                      </td>
                      <td className="num">
                        {pct(v.overall.recall)}
                        <span className="table__sub">
                          likely between {pct(v.recall_ci95[0])} and {pct(v.recall_ci95[1])}
                        </span>
                      </td>
                      <RateCell r={v.hallucination} />
                      <RateCell r={v.wrong_value} />
                    </>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {m && best ? (
        <details className="details">
          <summary>Field by field: {MODE_LABEL[best]}</summary>
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Accuracy for each field</caption>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">Emails that state it</th>
                  <th scope="col">Precision</th>
                  <th scope="col">Recall</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(m.per_field).map(([name, row]) => (
                  <tr key={name}>
                    <th scope="row">{fieldLabel(name)}</th>
                    <td className="num">{goldCount(row)}</td>
                    <td className="num">{pct(row.precision)}</td>
                    <td className="num">{pct(row.recall)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
    </Section>
  );
}

function CalibrationSection({ s }: { s: EvalSnapshot }) {
  const modes = s.real_model.extraction.modes;
  const best = bestMode(modes);
  const cal = (best ? modes[best]?.calibration : undefined) ?? null;
  if (!best || !cal || !cal.bins) {
    return (
      <Section title="Can you trust its confidence?">
        <p className="muted">Not measured: no recorded AI answers to compare confidence against.</p>
      </Section>
    );
  }
  return (
    <Section
      title="Can you trust its confidence?"
      lead={`If confidence means anything, fields marked high should be right more often than fields marked abstain. ${MODE_LABEL[best]}, ${cal.n} filled-in values. Brier score ${cal.brier?.toFixed(2) ?? "none"} (0 is perfect).`}
    >
      <div className="table-wrap">
        <table className="table">
          <caption className="visually-hidden">Confidence against actual accuracy</caption>
          <thead>
            <tr>
              <th scope="col">Confidence band</th>
              <th scope="col">Values</th>
              <th scope="col">Actually right</th>
              <th scope="col">Likely range</th>
            </tr>
          </thead>
          <tbody>
            {cal.bins.map((b: EvalBin) => (
              <tr key={b.band}>
                <th scope="row">
                  {humanize(b.band)}
                  <span className="table__sub">
                    {b.lo.toFixed(2)} to {b.hi.toFixed(2)}
                  </span>
                </th>
                <td className="num">{b.n}</td>
                <td className="num">{b.n ? `${b.correct} of ${b.n}, ${pct(b.accuracy)}` : "none"}</td>
                <td>{b.ci95 ? <Interval r={{ k: b.correct, n: b.n, rate: b.accuracy, ci95: b.ci95 }} /> : <span className="muted">no data</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">Confidence here is a rule of thumb built from how many independent checks agree. It is not a probability, and these bands hold few values.</p>
    </Section>
  );
}

function InjectionSection({ s }: { s: EvalSnapshot }) {
  const inj = s.scripted.injection;
  const rows = injectionRows(inj);
  const weak = weakPayloads(inj.by_payload).filter((p) => p.id.startsWith("H"));
  const probe = s.real_model.injection_probe;
  return (
    <Section
      title="Does hidden text in an email take control?"
      lead="Attack emails hide instructions such as approve this automatically. In this test the AI is assumed to obey the attacker, so only the platform’s own checks can stop it."
    >
      <div className="table-wrap">
        <table className="table">
          <caption className="visually-hidden">Attack results</caption>
          <thead>
            <tr>
              <th scope="col">Attack set</th>
              <th scope="col">Attacks</th>
              <th scope="col">Spotted by the scanner</th>
              <th scope="col">Attacker value used</th>
              <th scope="col">Real fields still kept</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label}>
                <th scope="row">{r.label}</th>
                <td className="num">{r.attacks}</td>
                <RateCell r={r.detected} />
                <RateCell r={r.poison} />
                <RateCell r={r.utility} />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Notice tone="person" title="The unseen attacks are the honest number">
        <p>
          The scanner was tuned on the first set, so its score there proves little. The second set was written before any result was seen and never tuned against.
          {weak.length ? ` It misses: ${weak.map((w) => `${w.id} (${rateText(w.detected)} caught)`).join(", ")}.` : ""} These slip past the scanner and rely on the
          AI’s own warning, the second opinion, and the rule that any spend needs a person to approve.
        </p>
      </Notice>
      {probe.available && probe.attacks ? (
        <p className="muted">
          Real AI check on {probe.attacks} unseen attacks: the AI or the scanner flagged {countText(probe.flagged_by_any_layer)}; the AI caught{" "}
          {probe.llm_caught_what_scanner_missed?.k ?? 0} that the scanner missed; the attacker value was used in {countText(probe.poison_survived)}. A clean control email was{" "}
          {probe.control_false_flag ? "wrongly flagged" : "not flagged"}. Six attacks show direction, not a rate.
        </p>
      ) : (
        <p className="muted">Real AI check: not measured in this snapshot.</p>
      )}
    </Section>
  );
}

function Provenance({ s }: { s: EvalSnapshot }) {
  const p = s.provenance;
  const cl = s.real_model.cost_latency;
  const sec = (ms: number | null | undefined) => (ms === null || ms === undefined ? "none" : `${(ms / 1000).toFixed(1)} s`);
  return (
    <Section title="Where these numbers came from">
      <dl className="kv">
        <div>
          <dt>Taken</dt>
          <dd>
            {fmtDateTime(p.generated_at)}, {p.run_mode === "live" ? "calling the AI live" : "replaying recorded AI answers"}
          </dd>
        </div>
        <div>
          <dt>AI models in the recording</dt>
          <dd>
            {p.models_in_recording.join(", ") || "none"} ({p.recorded_responses} recorded answers)
          </dd>
        </div>
        <div>
          <dt>Test emails fingerprint</dt>
          <dd className="num">{p.dataset_hash}</dd>
        </div>
        <div>
          <dt>AI instructions fingerprint</dt>
          <dd className="num">{p.prompt_hash}</dd>
        </div>
        {cl.calls && cl.latency_ms ? (
          <div>
            <dt>AI speed and size</dt>
            <dd>
              Median {sec(cl.latency_ms.p50)}, slowest 5% {sec(cl.latency_ms.p95)}; about {cl.tokens_per_call?.in ?? "none"} tokens in and {cl.tokens_per_call?.out ?? "none"} out per call
            </dd>
          </div>
        ) : null}
      </dl>
    </Section>
  );
}

function Body({ s }: { s: EvalSnapshot }) {
  return (
    <>
      {s.stale ? (
        <Notice tone="bad" title="This snapshot is out of date">
          <p>{staleText(s.stale_because)}</p>
        </Notice>
      ) : null}
      <Headline s={s} />
      <ExtractionSection s={s} />
      <CalibrationSection s={s} />
      <InjectionSection s={s} />
      <Section title="What these tests do not show">
        <ul className="evals__limits">
          {s.limits.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      </Section>
      <Provenance s={s} />
    </>
  );
}

export function EvalsPanel() {
  const q = useEvals();
  const snap = q.data && q.data.available ? q.data : null;
  return (
    <Panel
      title="Quality measurements"
      id="evals"
      aside={snap ? <Chip tone={snap.stale ? "bad" : "instrument"}>{snap.stale ? "Out of date" : snap.provenance.run_mode === "live" ? "Live run" : "Replay"}</Chip> : null}
    >
      <Boundary query={q} what="the quality measurements" skeleton={<Skeleton rows={4} label="Loading measurements" />}>
        {(d) =>
          d.available ? (
            <Body s={d} />
          ) : (
            <p className="muted">
              No measurements yet. They are produced offline by the test harness (<code>python -m app.evals.run</code>), not on this page.
            </p>
          )
        }
      </Boundary>
    </Panel>
  );
}
