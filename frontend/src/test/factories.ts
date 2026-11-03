/** Builders for API shapes. Every field has a plain default; a test overrides only what it is about. */
import type { Approval, AuditEvent, CaseDetail, CaseHead, CaseRow, Recommendation, User } from "../api/types";

export function user(over: Partial<User> = {}): User {
  return { id: "u-1", email: "priya.requester@demo.test", name: "Priya Nair", role: "requester", region: "West", ...over };
}

export function caseRow(over: Partial<CaseRow> = {}): CaseRow {
  return {
    id: "CASE-1",
    status: "AWAITING_APPROVAL",
    title: "Pune shelf shortage",
    intent: "supplier_delay",
    priority: "P2",
    risk_level: "High",
    degraded: false,
    needs_human: true,
    injection_flag: false,
    region: "West",
    deadline_at: null,
    created_at: "2026-11-03T06:00:00Z",
    updated_at: "2026-11-03T06:30:00Z",
    parent_id: null,
    duplicate_of: null,
    ...over,
  };
}

export function caseHead(over: Partial<CaseHead> = {}): CaseHead {
  return { ...caseRow(), version: 1, requester_id: "u-1", owner_id: null, closed_at: null, reopen_count: 0, ...over };
}

export function approval(over: Partial<Approval> = {}): Approval {
  return {
    id: 1,
    recommendation_id: 1,
    round: 1,
    tier: 1,
    required_role: "regional_head",
    approver_id: null,
    amount: 228000,
    status: "pending",
    reason: null,
    deadline_at: null,
    reminded_50: false,
    reminded_90: false,
    decided_at: null,
    approver_name: null,
    ...over,
  };
}

export function recommendation(over: Partial<Recommendation> = {}): Recommendation {
  return {
    id: 1,
    round: 1,
    options: [],
    chosen_option: null,
    total_cost: 0,
    rationale: null,
    evidence: {},
    inputs_used: 3,
    inputs_total: 3,
    sufficiency: "high",
    status: "proposed",
    superseded_by: null,
    created_at: "2026-11-03T06:10:00Z",
    ...over,
  };
}

export function detail(over: Partial<CaseDetail> = {}): CaseDetail {
  return {
    case: caseHead(),
    fields: [],
    facts: {},
    recommendations: [],
    approvals: [],
    policy: null,
    agent_messages: [],
    sources: [],
    related: { duplicate_of: null, parent: null, children: [], possible_duplicates: [] },
    ...over,
  };
}

export function auditEvent(over: Partial<AuditEvent> = {}): AuditEvent {
  return {
    seq: 1,
    ts: "2026-11-03T06:00:00Z",
    sim_ts: null,
    actor_type: "agent",
    actor_id: "intake",
    action: "extract",
    outcome: "success",
    severity: "info",
    reason_code: null,
    summary: "Extracted fields",
    diff: null,
    refs: null,
    error: null,
    not_fetched: null,
    model: null,
    trace_id: null,
    row_hash: "abc",
    ...over,
  };
}
