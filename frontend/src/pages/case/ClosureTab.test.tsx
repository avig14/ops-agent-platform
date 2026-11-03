import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Closure, ClosureItem } from "../../api/types";
import { caseHead, detail, user } from "../../test/factories";
import { mockApi, renderWithProviders } from "../../test/mockApi";
import { ClosureTab } from "./ClosureTab";

afterEach(() => vi.restoreAllMocks());

function item(over: Partial<ClosureItem>): ClosureItem {
  return { key: "k", label: "An item", kind: "manual", required: true, done: false, detail: "", by: null, by_name: null, at: null, note: null, ...over };
}

function show(items: ClosureItem[], over: Partial<Closure> = {}) {
  const status = over.status ?? "EXECUTING";
  const body: Closure = { status, reopen_count: 0, closed_at: null, epoch: 0, items, can_close: false, missing: [], latest_summary: null, ...over };
  mockApi({ "GET /api/cases/CASE-0001/closure": { body } });
  renderWithProviders(<ClosureTab detail={detail({ case: caseHead({ id: "CASE-0001", status }) })} user={user({ role: "regional_head" })} />);
}

describe("ClosureTab readiness chip", () => {
  it("says Closed on a closed case, never 'Not ready to close'", async () => {
    show([item({ done: true })], { status: "CLOSED", closed_at: "2026-11-03T17:00:00Z" });
    expect(await screen.findByText("Closed")).toBeInTheDocument();
    expect(screen.queryByText(/ready to close/i)).not.toBeInTheDocument();
  });

  it("says Ready to close on a live case with a full checklist", async () => {
    show([item({ done: true })], { can_close: true });
    expect(await screen.findByText("Ready to close")).toBeInTheDocument();
  });

  it("shows no readiness chip before the plan is approved", async () => {
    show([], { status: "AWAITING_APPROVAL" });
    expect(await screen.findByText(/closure opens once the plan is approved/i)).toBeInTheDocument();
    expect(screen.queryByText(/ready to close/i)).not.toBeInTheDocument();
  });
});

describe("ClosureTab done-by column", () => {
  it("shows the person's name, not their id", async () => {
    show([item({ key: "spend_recorded", label: "Spend recorded by finance", done: true, by: "u-fin", by_name: "Vikram Shah (Finance Director)" })]);
    expect(await screen.findByText("Vikram Shah (Finance Director)")).toBeInTheDocument();
    expect(screen.queryByText("u-fin")).not.toBeInTheDocument();
  });

  it("falls back to the id when the account is gone", async () => {
    show([item({ done: true, by: "u-gone", by_name: null })]);
    expect(await screen.findByText("u-gone")).toBeInTheDocument();
  });

  it("says where a self-ticked item came from, and that an open one has nobody", async () => {
    show([
      item({ key: "plan_approved", label: "Plan approved", kind: "derived", done: true }),
      item({ key: "resolution_confirmed", label: "Resolution confirmed" }),
    ]);
    expect(await screen.findByText("Checked from the audit trail")).toBeInTheDocument();
    expect(screen.getByText("nobody yet")).toBeInTheDocument();
  });
});
