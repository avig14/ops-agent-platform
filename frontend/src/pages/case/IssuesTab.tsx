import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { useEditFields, useLinkDuplicate, useNotDuplicate, useResolve } from "../../api/hooks";
import type { Conflict, PossibleDuplicate } from "../../api/types";
import { ActionError, Chip, Empty, Panel } from "../../components/ui";
import { Confirm } from "../../components/Confirm";
import { ageText, fmtPct, humanize } from "../../lib/format";
import { fieldLabel, FINISHED } from "../../lib/labels";
import { canChangeCase } from "../../lib/permissions";
import type { CaseTabProps } from "./shared";

export function IssuesTab({ detail, user }: CaseTabProps) {
  const c = detail.case;
  const f = detail.facts;
  const canAct = canChangeCase(user, c) && !FINISHED.has(c.status);
  const missing = f.missing_detail ?? (f.missing ?? []).map((field) => ({ field, why: "Not found in the message." }));
  const conflicts = f.conflicts ?? [];
  const warnings = f.warnings ?? [];
  const dups = detail.related.possible_duplicates;
  const rel = detail.related;
  const nothing =
    missing.length === 0 && conflicts.length === 0 && warnings.length === 0 && dups.length === 0 && !rel.parent && rel.children.length === 0 && !rel.duplicate_of;

  if (nothing) {
    return (
      <Panel>
        <Empty title="Nothing to fix">No missing facts, no conflicts, no duplicates. If something looks wrong, correct it on the Overview tab.</Empty>
      </Panel>
    );
  }

  return (
    <>
      {missing.length > 0 ? <MissingPanel caseId={c.id} missing={missing} canAct={canAct} /> : null}
      {conflicts.length > 0 ? <ConflictsPanel caseId={c.id} conflicts={conflicts} canAct={canAct} /> : null}
      {dups.length > 0 ? <DuplicatesPanel caseId={c.id} dups={dups} canAct={canAct} /> : null}
      {warnings.length > 0 ? (
        <Panel title="Warnings" id="warnings">
          <ul>
            {warnings.map((w) => (
              <li key={w.code + w.msg}>
                {w.msg} <span className="muted num">{w.code}</span>
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
      {rel.parent || rel.children.length > 0 || rel.duplicate_of ? (
        <Panel title="Linked cases" id="linked">
          <ul>
            {rel.duplicate_of ? (
              <li>
                Duplicate of <CaseLink id={rel.duplicate_of} />
              </li>
            ) : null}
            {rel.parent ? (
              <li>
                Split from <CaseLink id={rel.parent} />
              </li>
            ) : null}
            {rel.children.map((id) => (
              <li key={id}>
                Child case <CaseLink id={id} />
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </>
  );
}

function CaseLink({ id }: { id: string }) {
  return (
    <Link to={`/cases/${id}`} className="rowlink num">
      {id}
    </Link>
  );
}

function MissingPanel({ caseId, missing, canAct }: { caseId: string; missing: { field: string; why: string }[]; canAct: boolean }) {
  const edit = useEditFields(caseId);
  const [name, setName] = useState<string | null>(null);
  const [value, setValue] = useState("");

  function save(ev: FormEvent) {
    ev.preventDefault();
    if (!name || !value.trim()) return;
    edit.mutate({ fields: { [name]: value.trim() }, reason: "Supplied on the Issues tab" }, { onSuccess: () => { setName(null); setValue(""); } });
  }

  return (
    <Panel title="Missing information" id="missing" aside={<Chip tone="person">A person is needed</Chip>}>
      <p className="muted">The platform asked for these by email or in-app notice. You can also type the value here, or reply on the Overview tab.</p>
      <ul className="issues">
        {missing.map((m) => (
          <li key={m.field}>
            <p>
              <strong>{fieldLabel(m.field)}</strong> <span className="muted">{m.why}</span>
            </p>
            {canAct ? (
              name === m.field ? (
                <form className="inline-form" onSubmit={save}>
                  <label className="visually-hidden" htmlFor={`fill-${m.field}`}>
                    Value for {fieldLabel(m.field)}
                  </label>
                  <input id={`fill-${m.field}`} value={value} onChange={(e) => setValue(e.target.value)} maxLength={200} autoFocus />
                  <button type="submit" className="btn btn--primary" disabled={edit.isPending || !value.trim()}>
                    Save
                  </button>
                  <button type="button" className="btn" onClick={() => setName(null)}>
                    Cancel
                  </button>
                </form>
              ) : (
                <button type="button" className="btn" onClick={() => { setName(m.field); setValue(""); }}>
                  Fill in {fieldLabel(m.field).toLowerCase()}
                </button>
              )
            ) : null}
          </li>
        ))}
      </ul>
      <ActionError error={edit.error} what="save the value" />
    </Panel>
  );
}

function ConflictsPanel({ caseId, conflicts, canAct }: { caseId: string; conflicts: Conflict[]; canAct: boolean }) {
  const resolve = useResolve(caseId);
  return (
    <Panel title="Conflicting information" id="conflicts">
      <p className="muted">The email says one thing and the system says another. The platform will not guess. A person picks the source of truth.</p>
      <ul className="issues">
        {conflicts.map((k) => (
          <li key={k.field}>
            <h3>
              {fieldLabel(k.field)}{" "}
              {k.kind === "stale_snapshot" ? <Chip tone="person">Stale snapshot</Chip> : k.resolved ? <Chip tone="ok">Resolved</Chip> : <Chip tone="person">Unresolved</Chip>}
            </h3>
            <div className="compare">
              <div className="compare__col">
                <p className="compare__label">Email says</p>
                <p className="num compare__value">{k.email ?? "none"}</p>
              </div>
              <div className="compare__col">
                <p className="compare__label">System says</p>
                <p className="num compare__value">{k.system ?? "none"}</p>
                {k.system_source ? <p className="muted">{k.system_source}</p> : null}
                {k.snapshot_age_s !== null && k.snapshot_age_s !== undefined ? <p className="muted">Snapshot is {ageText(k.snapshot_age_s)} old.</p> : null}
              </div>
            </div>
            {k.resolved ? (
              <p>
                A person chose <strong>{k.resolved === "email" ? "the email value" : "the system value"}</strong>.
              </p>
            ) : k.kind === "conflict" && canAct ? (
              <div className="chips">
                <Confirm
                  label="Use the email value"
                  prompt={`Use ${k.email ?? "the email value"} for ${fieldLabel(k.field).toLowerCase()}? The plan is rebuilt.`}
                  confirmLabel="Yes, use email"
                  pending={resolve.isPending}
                  onConfirm={() => resolve.mutate({ field: k.field, source: "email" })}
                />
                <Confirm
                  label="Use the system value"
                  prompt={`Use ${k.system ?? "the system value"} for ${fieldLabel(k.field).toLowerCase()}? The plan is rebuilt.`}
                  confirmLabel="Yes, use system"
                  pending={resolve.isPending}
                  onConfirm={() => resolve.mutate({ field: k.field, source: "system" })}
                />
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      <ActionError error={resolve.error} what="resolve the conflict" />
    </Panel>
  );
}

function DuplicatesPanel({ caseId, dups, canAct }: { caseId: string; dups: PossibleDuplicate[]; canAct: boolean }) {
  const notDup = useNotDuplicate(caseId);
  const link = useLinkDuplicate(caseId);
  return (
    <Panel title="Possible duplicates" id="dups">
      <p className="muted">Matching is by facts and text similarity. The platform never merges on its own: a person confirms.</p>
      <div className="table-wrap">
        <table className="table">
          <caption className="visually-hidden">Possible duplicate cases</caption>
          <thead>
            <tr>
              <th scope="col">Case</th>
              <th scope="col">Match</th>
              <th scope="col">Why</th>
              <th scope="col">Differences</th>
            </tr>
          </thead>
          <tbody>
            {dups.map((d) => (
              <tr key={d.id}>
                <th scope="row">
                  <CaseLink id={d.id} />
                  {d.title ? <span className="table__sub">{d.title}</span> : null}
                </th>
                <td>
                  <Chip tone="instrument">
                    <span className="num">{fmtPct(d.score)}</span>
                  </Chip>
                  <span className="table__sub">{humanize(d.kind)}</span>
                </td>
                <td>{d.why ?? (d.equal && d.equal.length > 0 ? `Same ${d.equal.map(humanize).join(", ").toLowerCase()}.` : "Similar text.")}</td>
                <td>{d.conflicts && d.conflicts.length > 0 ? d.conflicts.map(humanize).join(", ") : <span className="muted">none</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {canAct ? (
        <div className="chips">
          <Confirm
            label="This is not a duplicate"
            prompt="Treat this as a separate request and continue the analysis?"
            confirmLabel="Yes, keep going"
            pending={notDup.isPending}
            onConfirm={() => notDup.mutate()}
          />
          {dups.slice(0, 3).map((d) => (
            <Confirm
              key={d.id}
              label={`Link as duplicate of ${d.id}`}
              tone="danger"
              prompt={`Mark this case as a duplicate of ${d.id}? It stops here and no second approval is raised.`}
              confirmLabel="Yes, link it"
              pending={link.isPending}
              onConfirm={() => link.mutate({ of: d.id })}
            />
          ))}
        </div>
      ) : null}
      <ActionError error={notDup.error ?? link.error} what="update the duplicate decision" />
    </Panel>
  );
}
