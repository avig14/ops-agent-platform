import { Link } from "react-router";
import { Chip, PageHeader, Panel } from "../components/ui";
import { POLICY_RULE } from "../lib/labels";

const AGENTS: { name: string; kind: "AI" | "Code" | "AI and code"; job: string; reads: string }[] = [
  { name: "Intake", kind: "AI", job: "Reads the message and pulls out facts. Every fact must carry a quote copied from the message.", reads: "The raw message only. It has no tools and cannot act." },
  { name: "Validator", kind: "Code", job: "Checks each fact against the supplier, store and item records. Finds missing, conflicting and duplicate facts.", reads: "Extracted facts and the master data." },
  { name: "Risk", kind: "AI and code", job: "Rules score the risk and the AI scores it independently. If they differ, both are shown and a person decides.", reads: "Validated facts and inventory numbers." },
  { name: "Recommend", kind: "AI and code", job: "Code builds and prices every option. The AI only chooses one and explains why.", reads: "Validated facts, risk, and the option list." },
  { name: "Policy", kind: "Code", job: "Decides who must approve and why. No AI is involved.", reads: "Risk, cost, flags and field confidence." },
  { name: "Comms", kind: "AI and code", job: "Drafts messages to suppliers and approvers from templates. Code checks every number in a draft.", reads: "Facts and the approved plan. Never internal costs for a supplier draft." },
  { name: "Reporting", kind: "AI and code", job: "Writes closing and management summaries from database numbers only.", reads: "Case and audit facts." },
];

const GATES: { check: string; catches: string }[] = [
  { check: "Quote check", catches: "A fact whose quote is not found in the message is dropped and marked missing." },
  { check: "Number check", catches: "A quantity that does not match the numbers in the quote is dropped." },
  { check: "Record check", catches: "A supplier, store or item that does not exist is flagged for a person." },
  { check: "Action check", catches: "An action the AI invents that is not on the option list is rejected." },
  { check: "Message check", catches: "A draft with a number not in the facts, a link, an e-mail address, an internal term or another supplier's name is replaced by the template." },
  { check: "Summary check", catches: "A summary with a number that is not in the database is replaced by a template." },
];

const LADDER = [
  { band: "Up to INR 50,000", who: "Manager in the region" },
  { band: "Up to INR 5,00,000", who: "Regional head" },
  { band: "Above INR 5,00,000", who: "Regional head and finance director, both" },
];

export function HowItWorksPage() {
  return (
    <div className="page page--narrow">
      <PageHeader
        title="How it works"
        lead="The rule behind every screen: the AI reads and suggests, and code decides. The AI cannot approve, send or change policy."
      />

      <Panel title="How the agents coordinate" id="agents">
        <p>
          Agents do not chat freely. Each one hands a typed result to the next through a coordinator written in plain code. The state of a case is stored in one place, the database, and every hand-over is saved. Open the{" "}
          <strong>Agents</strong> tab on any case to see who told whom what.
        </p>
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">The agents, what each one does and what it may read</caption>
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Made of</th>
                <th scope="col">Job</th>
                <th scope="col">May read</th>
              </tr>
            </thead>
            <tbody>
              {AGENTS.map((a) => (
                <tr key={a.name}>
                  <th scope="row">{a.name}</th>
                  <td>
                    <Chip tone={a.kind === "Code" ? "neutral" : "instrument"}>{a.kind}</Chip>
                  </td>
                  <td>{a.job}</td>
                  <td>{a.reads}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>
          Shared context is one record per case: the facts, the plan, the approvals and the hand-overs. Each agent reads only the inputs listed above. Raw message text is untrusted, so only Intake sees it. Later agents get checked facts, not the original words.
        </p>
        <p>
          When two agents disagree, for example the rule score and the AI score for risk, the higher risk is used for now, both answers are shown side by side, and a person resolves it.
        </p>
      </Panel>

      <Panel title="When a person must approve" id="approval">
        <p>The policy engine is code. It lists every rule that fired, and the Plan tab shows them. Any one of these needs a person:</p>
        <ul className="rules">
          {Object.entries(POLICY_RULE).map(([id, text]) => (
            <li key={id}>{text}</li>
          ))}
        </ul>
        <p>Who approves depends on the amount. This ladder is an assumption for the demo and is set in one place in the code.</p>
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">Approval authority by amount</caption>
            <thead>
              <tr>
                <th scope="col">Amount</th>
                <th scope="col">Approver</th>
              </tr>
            </thead>
            <tbody>
              {LADDER.map((l) => (
                <tr key={l.band}>
                  <th scope="row" className="num">
                    {l.band}
                  </th>
                  <td>{l.who}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>
          The requester cannot approve their own request. The AI has no approval permission at all. A late reply never counts as a yes: if nobody answers, reminders go out and the request moves up a tier.
        </p>
      </Panel>

      <Panel title="How confidence is decided" id="confidence">
        <p>Confidence is never just the AI saying it is sure. Each fact must pass a hard check first, then it is scored on three signals you can see on the Overview tab:</p>
        <ul>
          <li>Found in the message: the quote exists in the original text.</li>
          <li>Rule check agrees: a plain pattern match finds the same value.</li>
          <li>Second opinion agrees: a second AI pass reads the message and gets the same value.</li>
        </ul>
        <p>
          A fact that fails the quote check is dropped, not scored. Low confidence can only add review. It can never remove a required approval. For a plan, the AI abstains and a person decides when evidence is thin: the Plan tab shows how many inputs were used out of how many exist.
        </p>
        <p className="muted">These scores are rules of thumb. They are not yet calibrated against a labelled test set.</p>
      </Panel>

      <Panel title="What happens to wrong or unsupported AI output" id="gates">
        <p>Code checks every AI answer before anything uses it. A failed check is written to the audit trail. The answer is dropped, a fixed template or a person takes over, and nothing unchecked reaches an approver or a supplier.</p>
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">Checks applied to AI output</caption>
            <thead>
              <tr>
                <th scope="col">Check</th>
                <th scope="col">What it catches</th>
              </tr>
            </thead>
            <tbody>
              {GATES.map((g) => (
                <tr key={g.check}>
                  <th scope="row">{g.check}</th>
                  <td>{g.catches}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>
          If the AI is down, the case continues in Degraded mode. No spend is recommended, a person must review, and the Audit tab lists exactly which data could not be fetched. A message that tries to give orders to the system is flagged, and the parts that tried are ignored.
        </p>
        <p>
          A person can correct any fact. The correction is saved with who made it and what changed, and the plan is rebuilt from it.
        </p>
      </Panel>

      <Panel title="How actions are monitored and audited" id="audit">
        <p>
          Every action by an AI agent, a person or the system is written to an append-only audit trail with a Person, AI or System badge. Refused actions are recorded too, so a blocked approval attempt leaves a trace.
        </p>
        <ul>
          <li>Each entry holds a fingerprint of the entry before it. Changing or deleting one breaks all the later fingerprints.</li>
          <li>The Audit tab on every case, and the Admin page, run the check on demand.</li>
          <li>Failed calls record what was not fetched, so a plan always says how many inputs it used.</li>
          <li>Human corrections store the value before and after.</li>
        </ul>
        <p className="muted">
          This makes tampering visible. It does not make it impossible. A copy of the latest fingerprint kept somewhere else would close that gap.
        </p>
        <p>
          <Link to="/new" className="btn">
            Try a scenario
          </Link>
        </p>
      </Panel>
    </div>
  );
}
