import { useRef, type KeyboardEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useCase, useComms, useRetry } from "../api/hooks";
import type { CaseDetail, User } from "../api/types";
import { useUser } from "../components/Auth";
import { ActionError, Boundary, Chip, Notice, Skeleton, StatusChip } from "../components/ui";
import { issueCount, pendingDecisions } from "../lib/case";
import { dueLabel, humanize, timeLeft } from "../lib/format";
import { FINISHED, RISK_TONE, roleLabel, WORKING } from "../lib/labels";
import { canChangeCase } from "../lib/permissions";
import { AnalysisTab } from "./case/AnalysisTab";
import { ApprovalsTab } from "./case/ApprovalsTab";
import { AuditTab } from "./case/AuditTab";
import { ClosureTab } from "./case/ClosureTab";
import { CommsTab } from "./case/CommsTab";
import { IssuesTab } from "./case/IssuesTab";
import { OverviewTab } from "./case/OverviewTab";
import { PlanTab } from "./case/PlanTab";
import { TraceTab } from "./case/TraceTab";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "issues", label: "Issues" },
  { key: "analysis", label: "Analysis" },
  { key: "plan", label: "Plan" },
  { key: "approvals", label: "Approvals" },
  { key: "comms", label: "Messages" },
  { key: "trace", label: "Agents" },
  { key: "audit", label: "Audit" },
  { key: "closure", label: "Closure" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

function parseTab(raw: string | null): TabKey {
  return TABS.find((t) => t.key === raw)?.key ?? "overview";
}

export function CaseDetailPage() {
  const { id = "" } = useParams();
  const user = useUser();
  const query = useCase(id);

  return (
    <div className="page">
      <Boundary query={query} what="the case" skeleton={<Skeleton rows={8} label="Loading the case" />}>
        {(d) => <CaseView detail={d} user={user} />}
      </Boundary>
    </div>
  );
}

function CaseView({ detail, user }: { detail: CaseDetail; user: User }) {
  const c = detail.case;
  const [params, setParams] = useSearchParams();
  const tab = parseTab(params.get("tab"));
  const comms = useComms(c.id);
  const retry = useRetry(c.id);
  const listRef = useRef<HTMLDivElement>(null);

  function goTo(next: TabKey) {
    setParams(
      (prev) => {
        const n = new URLSearchParams(prev);
        if (next === "overview") n.delete("tab");
        else n.set("tab", next);
        n.delete("show");
        return n;
      },
      { replace: true },
    );
  }

  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = TABS.findIndex((t) => t.key === tab);
    let j = i;
    if (e.key === "ArrowRight") j = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") j = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = TABS.length - 1;
    else return;
    e.preventDefault();
    const next = TABS[j];
    if (!next) return;
    goTo(next.key);
    listRef.current?.querySelector<HTMLButtonElement>(`#tab-${next.key}`)?.focus();
  }

  const pending = pendingDecisions(detail);
  const draftCount = comms.data?.messages.filter((m) => m.status === "draft").length ?? 0;
  const counts: Partial<Record<TabKey, number>> = { issues: issueCount(detail), approvals: pending.length, comms: draftCount };
  const due = FINISHED.has(c.status) ? null : timeLeft(c.deadline_at);
  const title = c.title ?? (c.intent ? humanize(c.intent) : "Untitled request");
  const canRetry = canChangeCase(user, c);

  return (
    <>
      <header className="case-head">
        <p className="case-head__crumb">
          <Link to="/inbox" className="rowlink">
            Inbox
          </Link>{" "}
          <span className="num">{c.id}</span>
        </p>
        <h1>{title}</h1>
        <div className="chips">
          <StatusChip status={c.status} />
          {c.risk_level ? <Chip tone={RISK_TONE[c.risk_level] ?? "neutral"}>{c.risk_level} risk</Chip> : null}
          {c.priority ? <Chip>Priority {c.priority}</Chip> : null}
          {c.region ? <Chip>{c.region}</Chip> : null}
          <Chip title="Every change makes a new version. Two people editing at once cannot overwrite each other.">Version {c.version}</Chip>
          {due ? <span className={due.overdue ? "text-bad" : "muted"}>{dueLabel(due)}</span> : null}
        </div>
      </header>

      <div className="banners">
        {WORKING.has(c.status) ? (
          <div className="notice notice--instrument" role="status" aria-live="polite">
            <p className="notice__title">The AI is working</p>
            <div className="notice__body">{statusInfoText(c.status)} This page updates by itself.</div>
          </div>
        ) : null}
        {c.injection_flag ? (
          <Notice tone="bad" title="This message tried to give orders to the system">
            Those parts were ignored and no fact inside them was used. Nothing was approved or sent because of them. A person should triage the case.
          </Notice>
        ) : null}
        {c.degraded ? (
          <Notice tone="person" title="Analysis is incomplete">
            A dependency was down (the AI model or a data source). No spend is recommended and a person must review. The Audit tab lists exactly what could not be fetched.
            {canRetry && !FINISHED.has(c.status) ? (
              <p>
                <button type="button" className="btn" disabled={retry.isPending} onClick={() => retry.mutate()}>
                  {retry.isPending ? "Retrying" : "Retry the analysis"}
                </button>
              </p>
            ) : null}
            <ActionError error={retry.error} what="retry the analysis" />
          </Notice>
        ) : null}
        {c.duplicate_of ? (
          <Notice tone="neutral" title="This is a duplicate">
            <p>
              It repeats <Link to={`/cases/${c.duplicate_of}`} className="rowlink num">{c.duplicate_of}</Link>. No second approval is raised.
            </p>
          </Notice>
        ) : null}
        {c.status === "NEEDS_INFO" ? (
          <Notice tone="person" title="Waiting on a person">
            <p>
              Facts are missing or in conflict.{" "}
              <button type="button" className="linkbtn" onClick={() => goTo("issues")}>
                Open the Issues tab
              </button>
              .
            </p>
          </Notice>
        ) : null}
        {pending.length > 0 ? (
          <Notice tone="person" title={`Waiting on ${[...new Set(pending.map((a) => roleLabel(a.required_role)))].join(" and ")}`}>
            <p>
              <button type="button" className="linkbtn" onClick={() => goTo("approvals")}>
                Open the Approvals tab
              </button>{" "}
              to decide.
            </p>
          </Notice>
        ) : null}
      </div>

      <div className="tabs" role="tablist" aria-label="Case sections" ref={listRef} onKeyDown={onKey}>
        {TABS.map((t) => {
          const n = counts[t.key];
          return (
            <button
              key={t.key}
              id={`tab-${t.key}`}
              type="button"
              role="tab"
              className={`tab${tab === t.key ? " tab--on" : ""}`}
              aria-selected={tab === t.key}
              aria-controls={`panel-${t.key}`}
              tabIndex={tab === t.key ? 0 : -1}
              onClick={() => goTo(t.key)}
            >
              {t.label}
              {n ? <span className="tab__count num">{n}</span> : null}
            </button>
          );
        })}
      </div>

      <div id={`panel-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`} className="tabpanel">
        {tab === "overview" ? <OverviewTab detail={detail} user={user} /> : null}
        {tab === "issues" ? <IssuesTab detail={detail} user={user} /> : null}
        {tab === "analysis" ? <AnalysisTab detail={detail} user={user} /> : null}
        {tab === "plan" ? <PlanTab detail={detail} user={user} /> : null}
        {tab === "approvals" ? <ApprovalsTab detail={detail} user={user} /> : null}
        {tab === "comms" ? <CommsTab detail={detail} user={user} /> : null}
        {tab === "trace" ? <TraceTab detail={detail} user={user} /> : null}
        {tab === "audit" ? <AuditTab detail={detail} user={user} /> : null}
        {tab === "closure" ? <ClosureTab detail={detail} user={user} /> : null}
      </div>
    </>
  );
}

function statusInfoText(status: string): string {
  switch (status) {
    case "RECEIVED":
      return "The message is queued.";
    case "EXTRACTING":
      return "The intake agent is reading it and quoting its evidence.";
    case "ANALYZED":
      return "Risk is being scored by rules and by the AI.";
    case "RECOMMENDED":
      return "A plan is being drafted and checked.";
    case "REPLANNING":
      return "A new plan is being drafted without the rejected option.";
    default:
      return "";
  }
}
