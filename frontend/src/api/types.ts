/** Shapes of the FastAPI responses, written from real payloads (see backend/app/schemas.py and routes_*.py). */

export type Role = "requester" | "manager" | "regional_head" | "finance_director" | "admin" | "service";
export type Channel = "email" | "manual" | "supplier_update";
export type RiskLevel = "Low" | "Medium" | "High" | "Critical";

export interface User {
  id: string;
  email: string;
  name: string;
  role: Role;
  region: string | null;
}

export interface DemoUsers {
  password: string;
  users: Omit<User, "id">[];
}

export type CaseStatus =
  | "RECEIVED"
  | "EXTRACTING"
  | "NEEDS_INFO"
  | "ANALYZED"
  | "RECOMMENDED"
  | "AWAITING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "REPLANNING"
  | "EXECUTING"
  | "MONITORING"
  | "CLOSED"
  | "ESCALATED"
  | "CANCELLED"
  | "DUPLICATE"
  | "SUPERSEDED"
  | "SPLIT";

export interface CaseRow {
  id: string;
  status: CaseStatus;
  title: string | null;
  intent: string | null;
  priority: string | null;
  risk_level: RiskLevel | null;
  degraded: boolean;
  needs_human: boolean;
  injection_flag: boolean;
  region: string | null;
  deadline_at: string | null;
  created_at: string;
  updated_at: string;
  parent_id: string | null;
  duplicate_of: string | null;
}

export interface CaseHead extends CaseRow {
  version: number;
  requester_id: string | null;
  owner_id: string | null;
  closed_at: string | null;
  reopen_count: number;
}

export type FieldState = "ai" | "missing" | "confirmed" | "corrected";

export interface FieldRow {
  name: string;
  value: string | null;
  quote: string | null;
  confidence: number;
  signals: { rule?: boolean | null; grounded?: boolean | null; second_opinion?: boolean | null };
  state: FieldState;
}

export interface Conflict {
  field: string;
  email: string | number | null;
  system: string | number | null;
  system_source?: string | null;
  kind: "conflict" | "stale_snapshot";
  resolved?: string | null;
  snapshot_age_s?: number | null;
}

export interface Facts {
  po?: { id?: string; qty?: number; promised_date?: string } | null;
  item?: string | null;
  store?: string | null;
  intent?: string | null;
  missing?: string[];
  missing_detail?: { field: string; why: string }[];
  conflicts?: Conflict[];
  unresolved_conflicts?: string[];
  warnings?: { msg: string; code: string }[];
  shortfall?: number | null;
  qty_ordered?: number | null;
  qty_available?: number | null;
  delay_days?: number | null;
  days_to_need?: number | null;
  launch_gap_days?: number | null;
  needed_by?: string | null;
  approval_deadline?: string | null;
  additional_cost_stated?: number | null;
  deadline_passed_at_ingest?: boolean;
  supplier?: { id?: string; name?: string } | null;
  inventory?: { age_s: number; on_hand: number; location: string }[];
  inputs_used?: number;
  inputs_total?: number;
  data_notes?: string[];
  intake?: {
    mode?: string;
    degraded?: boolean;
    issues_found?: number;
    is_cancellation?: boolean;
    issues_after_merge?: number;
  };
}

export interface Option {
  action: string;
  description: string;
  units?: number;
  lead_time_days?: number | null;
  additional_cost: number;
  covers_shortfall?: boolean;
  feasible_by_need?: boolean;
  supplier?: string | null;
  item?: string | null;
  last_resort?: boolean;
}

export interface RiskEvidence {
  llm?: { risk?: string; impact?: string; urgency?: string; reasons?: string[]; missing_information?: string[] };
  rule?: { why?: string[]; level?: string; impact?: string; urgency?: string };
  level?: string;
  reasons?: string[];
  conflict?: boolean | Record<string, unknown> | null;
}

export interface Recommendation {
  id: number;
  round: number;
  options: Option[];
  chosen_option: string | null;
  total_cost: number | string;
  rationale: string | null;
  evidence: {
    risk?: RiskEvidence;
    ranking?: string[];
    /** Reason codes of checks that rejected the AI's answer. Empty when none did. */
    gate_events?: string[];
    risks_of_choice?: string[];
    rationale_source?: string;
  };
  inputs_used: number;
  inputs_total: number;
  sufficiency: string;
  status: "proposed" | "approved" | "rejected" | "superseded" | "abstained";
  superseded_by: number | null;
  created_at: string;
}

export type ApprovalStatus = "pending" | "approved" | "rejected" | "escalated" | "withdrawn" | "expired";

export interface Approval {
  id: number;
  recommendation_id: number;
  round: number;
  tier: number;
  required_role: Role;
  approver_id: string | null;
  amount: number | string;
  status: ApprovalStatus;
  reason: string | null;
  deadline_at: string | null;
  reminded_50: boolean;
  reminded_90: boolean;
  decided_at: string | null;
  approver_name: string | null;
}

export interface Policy {
  requires_human: boolean;
  rules_fired: { id: string; why: string }[];
  inputs: {
    risk?: string;
    tiers?: { role: Role; tier: number }[];
    amount?: number;
    degraded?: boolean;
    abstained?: boolean;
    agent_conflict?: boolean;
    field_confidence?: Record<string, number>;
  };
}

export interface AgentMessage {
  id: number;
  from_agent: string;
  to_agent: string | null;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface SourceMessage {
  role: string;
  id: number | string;
  channel: Channel;
  sender: string | null;
  subject: string | null;
  body: string;
  received_at: string;
}

export interface PossibleDuplicate {
  id: string;
  kind: string;
  score: number;
  text_sim?: number;
  equal?: string[];
  conflicts?: string[];
  why?: string;
  status?: string;
  title?: string | null;
}

export interface Related {
  duplicate_of: string | null;
  parent: string | null;
  children: string[];
  possible_duplicates: PossibleDuplicate[];
}

export interface CaseDetail {
  case: CaseHead;
  fields: FieldRow[];
  facts: Facts;
  recommendations: Recommendation[];
  approvals: Approval[];
  policy: Policy | null;
  agent_messages: AgentMessage[];
  sources: SourceMessage[];
  related: Related;
}

/** What POST /retry answers. `still_degraded` means the AI was still down and nothing was changed. */
export interface RetryResult {
  changed: boolean;
  recovered?: boolean;
  still_degraded?: boolean;
  failures?: string[];
  status?: string;
  version?: number;
  kept_human_values?: string[];
}

/** The case endpoint before it is tidied: `facts` is null until intake has run. See normaliseDetail. */
export type RawCaseDetail = Omit<CaseDetail, "facts"> & { facts: Facts | null };

export type ActorType = "human" | "agent" | "system";

export interface AuditEvent {
  seq: number;
  ts: string;
  sim_ts: string | null;
  actor_type: ActorType;
  actor_id: string;
  action: string;
  outcome: string;
  severity: string;
  reason_code: string | null;
  summary: string;
  diff: unknown;
  refs: Record<string, unknown> | null;
  error: { class?: string | null; provider?: string; http_status?: number; request_id?: string; msg?: string } | null;
  not_fetched: { source: string; operation?: string; reason_code?: string; affected?: string[] | string }[] | null;
  model: string | null;
  trace_id: string | null;
  row_hash: string;
}

export interface ChainStatus {
  chain_key: string;
  ok: boolean;
  checked: number;
  first_bad_seq: number | null;
  problem: string | null;
  head_hash: string | null;
}

export interface AuditResponse {
  events: AuditEvent[];
  chain: ChainStatus;
}

export type MessageStatus =
  | "draft"
  | "pending"
  | "sending"
  | "sent"
  | "failed"
  | "delivery_unknown"
  | "dead"
  | "cancelled";

export interface OutMessage {
  id: number;
  kind: string;
  recipient: string;
  audience?: string;
  subject: string;
  body: string;
  status: MessageStatus;
  attempts?: number;
  max_attempts?: number;
  next_attempt_at?: string | null;
  last_error?: string | null;
  reason_code?: string | null;
  source?: string;
  edited?: boolean;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  created_at?: string;
  sent_at: string | null;
  delivered_at: string | null;
}

export interface Notice {
  id: number;
  kind: string;
  recipient: string;
  subject: string;
  body: string;
  channel: string;
  delivered_at: string | null;
}

export interface CommsResponse {
  messages: OutMessage[];
  notices: Notice[];
}

export interface ClosureItem {
  key: string;
  label: string;
  kind: "derived" | "manual";
  required: boolean;
  done: boolean;
  detail: string;
  by: string | null;
  /** The person's name; null for derived items or when the account no longer exists. */
  by_name: string | null;
  at: string | null;
  note: string | null;
}

export interface SummaryContent {
  headline: string;
  what_happened: string;
  decisions: string[];
  open_items: string[];
}

export interface Summary {
  id: number;
  kind: string;
  content: SummaryContent;
  source: "llm" | "template";
  metrics_hash: string;
  created_at: string;
}

export interface Closure {
  status: CaseStatus;
  reopen_count: number;
  closed_at: string | null;
  epoch: number;
  items: ClosureItem[];
  can_close: boolean;
  missing: string[];
  latest_summary: Summary | null;
}

export interface MailboxMessage {
  id: number;
  message_id: string;
  case_id: string | null;
  kind: string;
  recipient: string;
  subject: string;
  body: string;
  channel: string;
  delivered_at: string | null;
  read_at: string | null;
}

export interface Mailbox {
  messages: MailboxMessage[];
  unread: number;
}

export interface AttentionItem {
  case_id: string;
  title: string | null;
  reasons: string[];
}

export interface ManagementReport {
  scope: string;
  currency: string;
  generated_at: string;
  cases: { total: number; open: number; closed: number; cancelled: number; by_status: Record<string, number> };
  open_by_risk: Record<string, number>;
  open_p1: number;
  needs_human: number;
  degraded: number;
  injection_flagged: number;
  approvals: { pending: number; overdue: number; approved: number; rejected: number; avg_decision_hours: number | null };
  spend_inr: { approved: number; pending_approval: number };
  comms: { sent: number; waiting: number; drafts: number; failed: number; dead: number };
  closure: { closed: number; avg_cycle_hours: number | null; reopened: number };
  attention: AttentionItem[];
}

export interface Narrative {
  id: number;
  kind: string;
  content: SummaryContent;
  source: "llm" | "template";
  metrics_hash: string;
  created_at: string;
  gate_events: string[];
  generated_at: string;
}

export interface Scenario {
  id: string;
  edge_case: string | null;
  title: string;
  description: string;
  expected: string;
  channel: Channel;
  subject: string | null;
  body: string;
  setup: string[];
  steps: string[];
}

export interface LlmStatus {
  mode: string | null;
  models: { model: string; circuit: string; failures: number }[];
  calls_today: number;
  chaos: string | null;
}

export interface AuditVerify {
  ok: boolean;
  chains: ChainStatus[];
}

export interface ClockState {
  enabled: boolean;
  offset_seconds: number;
  now: string;
}

export interface OutboxStats {
  by_status: Record<string, number>;
  oldest_due_s: number | null;
  fault: { mode: string | null; remaining: number };
}

export interface Accepted {
  case_id: string;
  created: boolean;
  status: CaseStatus;
}

// ───────────── evaluation snapshot (GET /api/admin/evals) ─────────────
export interface EvalRate {
  k: number;
  n: number;
  rate: number | null;
  ci95: [number, number];
}

export interface EvalPrf {
  tp: number;
  fp: number;
  fn: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
}

export interface EvalFieldRow extends EvalPrf {
  tn: number;
  wrong: number;
}

export interface EvalBin {
  band: string;
  lo: number;
  hi: number;
  n: number;
  correct: number;
  mean_confidence: number | null;
  accuracy: number | null;
  ci95: [number, number] | null;
}

export interface EvalCalibration {
  n: number;
  brier?: number | null;
  bins?: EvalBin[];
  risk_coverage?: { threshold: number; kept: number; coverage: number | null; precision: number | null; ci95: [number, number] | null }[];
}

export interface EvalMode {
  scored: number;
  not_scored: { id: string; error: string }[];
  per_field: Record<string, EvalFieldRow>;
  overall: EvalPrf & { tn: number; wrong: number };
  precision_ci95: [number, number];
  recall_ci95: [number, number];
  hallucination: EvalRate;
  wrong_value: EvalRate;
  injection_flag_accuracy: EvalRate;
  cancellation_accuracy: EvalRate;
  calibration: EvalCalibration;
}

export interface EvalInjectionGroup {
  attacks: number;
  detected: EvalRate;
  evaded: EvalRate;
  attack_success_llm_path: EvalRate;
  attack_success_rules_path: EvalRate;
  utility_under_attack: EvalRate;
}

export interface EvalProbe {
  available: boolean;
  attacks?: number;
  not_scored?: { id: string; error: string }[];
  flagged_by_any_layer?: EvalRate;
  scanner_missed?: number;
  llm_caught_what_scanner_missed?: EvalRate;
  poison_survived?: EvalRate;
  control_false_flag?: boolean | null;
}

export interface EvalSnapshot {
  available: true;
  stale: boolean;
  stale_because: string[];
  provenance: {
    generated_at: string;
    run_mode: string;
    dataset_hash: string;
    prompt_hash: string;
    models_in_recording: string[];
    recorded_responses: number;
    api_calls_this_run: number;
    git_sha: string | null;
  };
  scripted: {
    injection: {
      overall: EvalInjectionGroup;
      dev: EvalInjectionGroup;
      holdout: EvalInjectionGroup;
      by_wrapper: Record<string, EvalInjectionGroup>;
      by_payload: Record<string, EvalInjectionGroup>;
    };
    false_positives: { checked: number; false_positives: EvalRate };
    gates: { cases: { id: string; why: string; status: "pass" | "fail" | "limitation" }[]; passed: number; of: number; limitations: string[] };
    policy: { checks: number; passed: number; failed: string[] };
    risk: { checks: number; passed: number; failed: string[] };
    metamorphic: Record<string, EvalRate | Record<string, string[]>>;
  };
  real_model: {
    extraction: { cases: number; modes: Record<string, EvalMode> };
    injection_probe: EvalProbe;
    cost_latency: {
      calls: number;
      ok?: number;
      failed?: number;
      latency_ms?: { p50: number | null; p95: number | null; max: number | null };
      tokens_per_call?: { in: number | null; out: number | null };
      models?: string[];
    };
  };
  limits: string[];
}

export type EvalsResponse = EvalSnapshot | { available: false; hint?: string };
