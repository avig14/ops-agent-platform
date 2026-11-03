/** TanStack Query hooks. One place knows every endpoint; screens only call hooks. */
import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { normaliseDetail } from "../lib/case";
import { FINISHED, WORKING } from "../lib/labels";
import { api, ApiError } from "./client";
import type {
  Accepted, AuditResponse, AuditVerify, CaseRow, Channel, ClockState, Closure, CommsResponse, DemoUsers, EvalsResponse,
  LlmStatus, Mailbox, ManagementReport, Narrative, OutboxStats, RawCaseDetail, RetryResult, Scenario, Summary, User,
} from "./types";

// ───────────── session ─────────────

/** null means "not signed in". Any other failure is a real error and surfaces. */
export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async (): Promise<User | null> => {
      try {
        return await api<User>("/api/auth/me");
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null;
        throw err;
      }
    },
    retry: false,
    staleTime: 60_000,
  });
}

export function useDemoUsers() {
  return useQuery({ queryKey: ["demo-users"], queryFn: () => api<DemoUsers>("/api/auth/demo-users"), staleTime: Infinity });
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { email: string; password: string }) => api<User>("/api/auth/login", { method: "POST", body: v }),
    onSuccess: (user) => {
      qc.clear();
      qc.setQueryData(["me"], user);
    },
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
    onSettled: () => {
      qc.clear();
      qc.setQueryData(["me"], null);
    },
  });
}

// ───────────── cases ─────────────

export function useCases(status: string | undefined) {
  return useQuery({
    queryKey: ["cases", status ?? "all"],
    queryFn: () => api<{ cases: CaseRow[] }>("/api/cases", { query: { status } }).then((r) => r.cases),
    placeholderData: keepPreviousData,
    // The list moves when AI agents work in the background.
    refetchInterval: (q) => (q.state.data?.some((c) => WORKING.has(c.status)) ? 2500 : 10_000),
  });
}

export function useCase(id: string) {
  return useQuery({
    queryKey: ["case", id],
    queryFn: async () => normaliseDetail(await api<RawCaseDetail>(`/api/cases/${encodeURIComponent(id)}`)),
    retry: (count, err) => !(err instanceof ApiError && [401, 403, 404].includes(err.status)) && count < 2,
    refetchInterval: (q) => {
      const s = q.state.data?.case.status;
      if (!s) return false;
      if (WORKING.has(s)) return 2000;
      return FINISHED.has(s) ? false : 8000;
    },
  });
}

export function useAudit(id: string) {
  return useQuery({ queryKey: ["audit", id], queryFn: () => api<AuditResponse>(`/api/cases/${encodeURIComponent(id)}/audit`) });
}

export function useComms(id: string) {
  return useQuery({
    queryKey: ["comms", id],
    queryFn: () => api<CommsResponse>(`/api/cases/${encodeURIComponent(id)}/communications`),
    refetchInterval: (q) => (q.state.data?.messages.some((m) => ["pending", "sending", "failed"].includes(m.status)) ? 3000 : false),
  });
}

export function useClosure(id: string) {
  return useQuery({ queryKey: ["closure", id], queryFn: () => api<Closure>(`/api/cases/${encodeURIComponent(id)}/closure`) });
}

export function useSummary(id: string, enabled: boolean) {
  return useQuery({
    queryKey: ["summary", id],
    enabled,
    queryFn: () => api<{ summary: Summary | null }>(`/api/cases/${encodeURIComponent(id)}/summary`).then((r) => r.summary),
  });
}

/** Everything a case change can touch. Cheap: only mounted queries refetch. */
export function invalidateCase(qc: QueryClient, id: string): Promise<unknown> {
  return Promise.all(
    ["case", "audit", "comms", "closure", "summary"].map((k) => qc.invalidateQueries({ queryKey: [k, id] })),
  ).then(() => Promise.all(["cases", "report", "mailbox"].map((k) => qc.invalidateQueries({ queryKey: [k] }))));
}

export interface NewRequest {
  channel: Channel;
  sender?: string;
  subject?: string;
  body: string;
}

export function useCreateCase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: NewRequest) =>
      api<Accepted>("/api/cases", { method: "POST", body: { ...v, kind: "new", message_id: crypto.randomUUID() } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cases"] }),
  });
}

/** Run one action against a case, then refresh everything it can change. */
function useCaseAction<V, R = unknown>(caseId: string, fn: (v: V) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSettled: () => invalidateCase(qc, caseId) });
}

const base = (id: string) => `/api/cases/${encodeURIComponent(id)}`;

export function useDecision(id: string) {
  return useCaseAction(id, (v: { approvalId: number; decision: "approve" | "reject"; reason?: string }) =>
    api(`${base(id)}/approvals/${v.approvalId}/decision`, { method: "POST", body: { decision: v.decision, reason: v.reason } }));
}

export function useEditFields(id: string) {
  return useCaseAction(id, (v: { fields: Record<string, string>; reason?: string }) =>
    api(`${base(id)}/fields`, { method: "PATCH", body: v }));
}

export function useMessage(id: string) {
  return useCaseAction(id, (v: { kind: "reply" | "revision" | "cancel"; channel: Channel; body: string; subject?: string }) =>
    api(`${base(id)}/messages`, { method: "POST", body: { ...v, message_id: crypto.randomUUID() } }));
}

export function useResolve(id: string) {
  return useCaseAction(id, (v: { field: string; source: "email" | "system" }) =>
    api(`${base(id)}/resolve`, { method: "POST", body: v }));
}

export function useNotDuplicate(id: string) {
  return useCaseAction(id, () => api(`${base(id)}/not-duplicate`, { method: "POST" }));
}

export function useLinkDuplicate(id: string) {
  return useCaseAction(id, (v: { of: string }) => api(`${base(id)}/link-duplicate`, { method: "POST", body: v }));
}

export function useRetry(id: string) {
  return useCaseAction(id, () => api<RetryResult>(`${base(id)}/retry`, { method: "POST" }));
}

export type ClosureAction = "start" | "monitor" | "close" | "summary";

export function useClosureAction(id: string) {
  return useCaseAction(id, (a: ClosureAction) => api(`${base(id)}/closure/${a}`, { method: "POST" }));
}

export function useTickItem(id: string) {
  return useCaseAction(id, (v: { key: string; note?: string }) =>
    api(`${base(id)}/closure/items/${encodeURIComponent(v.key)}`, { method: "POST", body: { note: v.note } }));
}

export function useReopen(id: string) {
  return useCaseAction(id, (v: { reason: string }) => api(`${base(id)}/closure/reopen`, { method: "POST", body: v }));
}

export function useOutboxAction(caseId: string) {
  return useCaseAction(caseId, (v: { id: number; action: "retry" | "release" | "discard"; subject?: string; body?: string; reason?: string }) => {
    const { id, action, ...rest } = v;
    return api(`/api/outbox/${id}/${action}`, { method: "POST", body: action === "retry" ? undefined : rest });
  });
}

// ───────────── mailbox, reports, scenarios ─────────────

export function useMailbox(scope: "mine" | "all", unread: boolean) {
  return useQuery({
    queryKey: ["mailbox", scope, unread],
    queryFn: () => api<Mailbox>("/api/mailbox", { query: { scope, unread: unread || undefined } }),
    refetchInterval: 10_000,
  });
}

export function useUnreadCount(enabled: boolean) {
  return useQuery({
    queryKey: ["mailbox", "unread-count"],
    enabled,
    queryFn: () => api<Mailbox>("/api/mailbox", { query: { unread: true } }).then((r) => r.unread),
    refetchInterval: 10_000,
  });
}

export function useMarkRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api(`/api/mailbox/${id}/read`, { method: "POST" }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["mailbox"] }),
  });
}

export function useReport(enabled: boolean) {
  return useQuery({
    queryKey: ["report"],
    enabled,
    queryFn: () => api<ManagementReport>("/api/reports/management"),
    refetchInterval: 15_000,
  });
}

export function useNarrative() {
  return useMutation({ mutationFn: () => api<Narrative>("/api/reports/management/narrative", { method: "POST" }) });
}

export function useScenarios() {
  return useQuery({
    queryKey: ["scenarios"],
    queryFn: () => api<{ scenarios: Scenario[] }>("/api/scenarios").then((r) => r.scenarios),
    staleTime: Infinity,
  });
}

// ───────────── admin ─────────────

export function useLlmStatus() {
  return useQuery({ queryKey: ["admin", "llm"], queryFn: () => api<LlmStatus>("/api/admin/llm"), refetchInterval: 5000 });
}

export function useOutboxStats() {
  return useQuery({ queryKey: ["admin", "outbox"], queryFn: () => api<OutboxStats>("/api/admin/outbox/stats"), refetchInterval: 5000 });
}

export function useClock() {
  return useQuery({ queryKey: ["admin", "clock"], queryFn: () => api<ClockState>("/api/admin/clock") });
}

export function useEvals() {
  return useQuery({ queryKey: ["admin", "evals"], queryFn: () => api<EvalsResponse>("/api/admin/evals"), staleTime: 60_000 });
}

export function useVerifyAudit() {
  return useMutation({ mutationFn: () => api<AuditVerify>("/api/admin/audit/verify") });
}

export function useSetChaos() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (mode: string | null) => api<LlmStatus>("/api/admin/chaos", { method: "POST", body: { mode } }),
    onSuccess: (d) => qc.setQueryData(["admin", "llm"], d),
  });
}

export function useSetCommsChaos() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { mode: string | null; count?: number }) => api("/api/admin/comms-chaos", { method: "POST", body: v }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "outbox"] }),
  });
}

export function useSetClock() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (offset_seconds: number) => api<ClockState>("/api/admin/clock", { method: "POST", body: { offset_seconds } }),
    onSuccess: (d) => qc.setQueryData(["admin", "clock"], d),
  });
}

export function useAdminRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (what: "sweep" | "reminders" | "outbox/run") => api<Record<string, unknown>>(`/api/admin/${what}`, { method: "POST" }),
    onSettled: () => qc.invalidateQueries(),
  });
}
