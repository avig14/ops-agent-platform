/** Words and tones for every enum the API returns. Tone decides colour, never the other way round. */
import type { ApprovalStatus, CaseStatus, MessageStatus, Role } from "../api/types";

/** person = waits on a human, instrument = a machine reading or action, ok/bad = outcome, neutral = settled. */
export type Tone = "neutral" | "person" | "instrument" | "ok" | "bad";

export const STATUS: Record<CaseStatus, { label: string; tone: Tone; hint: string }> = {
  RECEIVED: { label: "Received", tone: "instrument", hint: "Queued for the AI to read." },
  EXTRACTING: { label: "Reading", tone: "instrument", hint: "The intake agent is extracting facts." },
  NEEDS_INFO: { label: "Needs info", tone: "person", hint: "A person must supply missing or conflicting facts." },
  ANALYZED: { label: "Assessing", tone: "instrument", hint: "Risk is being scored." },
  RECOMMENDED: { label: "Planning", tone: "instrument", hint: "A plan has been drafted." },
  AWAITING_APPROVAL: { label: "Awaiting approval", tone: "person", hint: "A named approver must decide." },
  ESCALATED: { label: "Escalated", tone: "person", hint: "The approval deadline passed; it moved up the ladder." },
  APPROVED: { label: "Approved", tone: "ok", hint: "All required approvals are in." },
  REJECTED: { label: "Rejected", tone: "bad", hint: "An approver rejected the plan." },
  REPLANNING: { label: "Re-planning", tone: "instrument", hint: "The AI is drafting a new plan." },
  EXECUTING: { label: "Executing", tone: "ok", hint: "The plan is being carried out." },
  MONITORING: { label: "Monitoring", tone: "ok", hint: "Watching for the outcome." },
  CLOSED: { label: "Closed", tone: "neutral", hint: "Finished and summarised." },
  CANCELLED: { label: "Cancelled", tone: "neutral", hint: "The request was withdrawn." },
  DUPLICATE: { label: "Duplicate", tone: "neutral", hint: "Linked to an earlier case." },
  SUPERSEDED: { label: "Superseded", tone: "neutral", hint: "Replaced by a newer case." },
  SPLIT: { label: "Split", tone: "neutral", hint: "Divided into separate child cases." },
};

/** The AI is mid-pipeline: poll fast. */
export const WORKING: ReadonlySet<CaseStatus> = new Set(["RECEIVED", "EXTRACTING", "ANALYZED", "RECOMMENDED", "REPLANNING"]);
/** Nothing will change on its own. */
export const FINISHED: ReadonlySet<CaseStatus> = new Set(["CLOSED", "CANCELLED", "DUPLICATE", "SUPERSEDED", "SPLIT"]);

export const STATUS_ORDER: CaseStatus[] = [
  "NEEDS_INFO", "AWAITING_APPROVAL", "ESCALATED", "REPLANNING", "RECEIVED", "EXTRACTING", "ANALYZED", "RECOMMENDED",
  "APPROVED", "EXECUTING", "MONITORING", "REJECTED", "CLOSED", "CANCELLED", "DUPLICATE", "SUPERSEDED", "SPLIT",
];

export function statusInfo(s: string): { label: string; tone: Tone; hint: string } {
  return STATUS[s as CaseStatus] ?? { label: s, tone: "neutral", hint: "" };
}

export const ROLE: Record<Role, string> = {
  requester: "Requester",
  manager: "Manager",
  regional_head: "Regional head",
  finance_director: "Finance director",
  admin: "Admin",
  service: "AI service",
};

export function roleLabel(r: string): string {
  return ROLE[r as Role] ?? r;
}

export const APPROVER_ROLES: ReadonlySet<Role> = new Set(["manager", "regional_head", "finance_director"]);

export const FIELD_LABEL: Record<string, string> = {
  supplier: "Supplier",
  item: "Item",
  quantity_affected: "Quantity affected",
  quantity_available: "Quantity available",
  delay_days: "Delay (days)",
  location: "Location",
  needed_by: "Needed by",
  approval_deadline: "Approval deadline",
  additional_cost: "Additional cost (INR)",
};

export function fieldLabel(name: string): string {
  return FIELD_LABEL[name] ?? name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export const RISK_TONE: Record<string, Tone> = { Low: "neutral", Medium: "neutral", High: "person", Critical: "bad" };

export const APPROVAL_TONE: Record<ApprovalStatus, Tone> = {
  pending: "person",
  approved: "ok",
  rejected: "bad",
  escalated: "person",
  withdrawn: "neutral",
  expired: "bad",
};

export const MESSAGE: Record<MessageStatus, { label: string; tone: Tone }> = {
  draft: { label: "Draft: needs review", tone: "person" },
  pending: { label: "Queued", tone: "instrument" },
  sending: { label: "Sending", tone: "instrument" },
  sent: { label: "Sent", tone: "ok" },
  failed: { label: "Failed, will retry", tone: "bad" },
  delivery_unknown: { label: "Delivery unconfirmed", tone: "person" },
  dead: { label: "Failed for good", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

/** Plain-English meaning of each policy rule id (from backend/app/policy.py). */
export const POLICY_RULE: Record<string, string> = {
  SPEND_GT_ZERO: "Any extra spend needs a person's approval.",
  RISK_HIGH_OR_CRITICAL: "High and Critical risk always need a person.",
  INJECTION_FLAG: "The request contained text that tried to instruct the system.",
  DEGRADED_MODE: "A dependency was down, so the analysis is incomplete.",
  AGENT_CONFLICT: "The risk agents disagreed; a person breaks the tie.",
  DATA_CONFLICT_UNRESOLVED: "Email and system data disagree and nobody has chosen yet.",
  MISSING_REQUIRED_INFO: "Required facts are missing.",
  AI_ABSTAINED: "The AI declined to guess because evidence was too thin.",
  LOW_CONFIDENCE: "A critical field fell below the confidence floor for this stake level.",
  CANCEL_AFTER_APPROVAL: "Cancelling after approval started needs a person.",
  REVISION_AFTER_APPROVAL: "A supplier changed the facts after approval; earlier approvals no longer hold.",
  POSSIBLE_DUPLICATE: "It may repeat an earlier request; a person must confirm.",
  PRIOR_REJECTION: "An approver rejected an earlier plan; the revised plan needs a decision.",
  FIELD_DISAGREEMENT: "Two readers of the request disagree on a key value; a person confirms it.",
  SHORTFALL_UNVERIFIED: "The shortfall could not be worked out, so the plan cannot be trusted yet.",
  SHORTFALL_NOT_COVERED: "No option closes the shortfall before the need date.",
  ESCALATION_RECOMMENDED: "The plan is to escalate, so management must decide.",
  DATE_UNREADABLE: "A need date was given but could not be read, so timing is unverified.",
  SUBSTITUTE_PRODUCT: "The plan swaps the requested product for a different one; that needs a person's agreement.",
};

/** What each code in a recommendation's `gate_events` means (codes from backend/app/codes.py and recommend.py). */
export const GATE_EVENT: Record<string, string> = {
  GATE_UNGROUNDED: "A value had no quote in the original message, so it was dropped.",
  GATE_NUMERIC_MISMATCH: "A number did not match what code computed, so the answer was dropped.",
  GATE_UNKNOWN_ENTITY: "The answer named a supplier, store or item that does not exist.",
  GATE_BAD_ACTION: "The AI picked an action that was not one of the options code built.",
  GATE_COMMS_NUMBER: "The wording used a number that is not in the case facts.",
  GATE_COMMS_LEAK: "The wording leaked internal cost or margin detail.",
  llm_unavailable: "The AI service could not be reached, so code took over.",
};

export interface GateView {
  code: string;
  text: string;
  /** "blocked" = a check rejected the AI answer; "fallback" = the AI was not used at all. */
  kind: "blocked" | "fallback";
}

export function gateView(code: string): GateView {
  return {
    code,
    text: GATE_EVENT[code] ?? "A check rejected the AI's answer.",
    kind: code.startsWith("GATE_") ? "blocked" : "fallback",
  };
}
