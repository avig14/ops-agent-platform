import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CaseStatus } from "../../api/types";
import { approval, caseHead, detail, user } from "../../test/factories";
import { mockApi, renderWithProviders } from "../../test/mockApi";
import { DegradedNotice } from "./DegradedNotice";

afterEach(() => vi.restoreAllMocks());

const RETRY = "POST /api/cases/CASE-1/retry";

function show(over: Parameters<typeof caseHead>[0] = {}, extra: Parameters<typeof detail>[0] = {}) {
  const d = detail({ case: caseHead({ degraded: true, ...over }), ...extra });
  return renderWithProviders(<DegradedNotice detail={d} user={user()} />);
}

describe("DegradedNotice visibility", () => {
  it("shows nothing on a healthy case", () => {
    show({ degraded: false });
    expect(screen.queryByText(/analysis is incomplete/i)).not.toBeInTheDocument();
  });

  it.each<CaseStatus>(["AWAITING_APPROVAL", "NEEDS_INFO"])("offers retry while %s", (status) => {
    show({ status });
    expect(screen.getByRole("button", { name: "Retry the analysis" })).toBeEnabled();
  });

  it.each<CaseStatus>(["APPROVED", "ESCALATED", "EXECUTING", "CLOSED", "REJECTED", "CANCELLED"])(
    "explains the gap but offers no retry in %s",
    (status) => {
      show({ status });
      expect(screen.getByText(/analysis is incomplete/i)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    },
  );

  it("offers no retry once an approver has said yes in the newest round", () => {
    show({}, { approvals: [approval({ status: "approved", approver_id: "u-9" })] });
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });

  it("still offers retry when only an older round was approved", () => {
    show({}, { approvals: [approval({ id: 1, round: 1, status: "approved", approver_id: "u-9" }), approval({ id: 2, round: 2 })] });
    expect(screen.getByRole("button", { name: "Retry the analysis" })).toBeInTheDocument();
  });

  it("offers no retry to someone who does not own or cover the case", () => {
    const d = detail({ case: caseHead({ degraded: true, requester_id: "u-other" }) });
    renderWithProviders(<DegradedNotice detail={d} user={user({ id: "u-1", role: "requester" })} />);
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });
});

describe("DegradedNotice answers", () => {
  it("says nothing changed when the AI is still down, and keeps the button", async () => {
    mockApi({ [RETRY]: { body: { changed: false, still_degraded: true, failures: ["LLM_OVERLOADED"], status: "AWAITING_APPROVAL" } } });
    show();
    await userEvent.click(screen.getByRole("button", { name: "Retry the analysis" }));
    expect(await screen.findByText(/still unavailable, so nothing changed/i)).toBeInTheDocument();
    expect(screen.getByText(/llm overloaded/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry the analysis" })).toBeInTheDocument();
  });

  it("keeps the recovery message after the degraded banner is gone", async () => {
    mockApi({ [RETRY]: { body: { changed: true, recovered: true, status: "AWAITING_APPROVAL", kept_human_values: ["delay_days"] } } });
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const wrap = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    );
    const sick = detail({ case: caseHead({ degraded: true }) });
    const { rerender } = render(<DegradedNotice detail={sick} user={user()} />, { wrapper: wrap });
    await userEvent.click(screen.getByRole("button", { name: "Retry the analysis" }));
    // the page refetches the case after the answer: the flag is now clear, the banner goes, the answer stays
    rerender(<DegradedNotice detail={detail({ case: caseHead({ degraded: false }) })} user={user()} />);
    expect(await screen.findByText(/analysis recovered/i)).toBeInTheDocument();
    expect(screen.getByText(/delay days/i)).toBeInTheDocument();
    expect(screen.queryByText(/analysis is incomplete/i)).not.toBeInTheDocument();
  });

  it.each([
    [429, "Please wait before retrying the analysis again"],
    [409, "An approver has already acted on this case"],
  ])("shows the server's %s refusal in plain words", async (status, error) => {
    mockApi({ [RETRY]: { status, body: { error, request_id: "req-1" } } });
    show();
    await userEvent.click(screen.getByRole("button", { name: "Retry the analysis" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(error);
    expect(screen.getByRole("alert")).toHaveTextContent("req-1");
  });

  it("sends one POST per click and disables the button while it runs", async () => {
    const { calls } = mockApi({ [RETRY]: { body: { changed: false, still_degraded: true } } });
    show();
    await userEvent.click(screen.getByRole("button", { name: "Retry the analysis" }));
    await screen.findByText(/still unavailable/i);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
});
