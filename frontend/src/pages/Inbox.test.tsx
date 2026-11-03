import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequireAuth } from "../components/Auth";
import { caseRow, user } from "../test/factories";
import { mockApi, renderWithProviders } from "../test/mockApi";
import { InboxPage } from "./Inbox";

afterEach(() => vi.restoreAllMocks());

const CASES = [
  caseRow({ id: "CASE-1", status: "CLOSED", priority: "P1", title: "Old closed one", region: "West" }),
  caseRow({ id: "CASE-2", status: "AWAITING_APPROVAL", priority: "P2", title: "Pune shelf shortage", region: "West" }),
  caseRow({ id: "CASE-3", status: "NEEDS_INFO", priority: "P1", title: "Missing supplier", region: "South", injection_flag: true }),
  caseRow({ id: "CASE-4", status: "EXECUTING", priority: "P3", title: "Plan running", region: "West", duplicate_of: "CASE-2", degraded: true }),
];

function renderInbox(opts: { cases?: unknown; role?: "requester" | "admin" } = {}) {
  const me = user({ role: opts.role ?? "requester", region: opts.role === "admin" ? null : "West" });
  const api = mockApi({
    "GET /api/auth/me": { body: me },
    "GET /api/cases": opts.cases === undefined ? { body: { cases: CASES } } : (opts.cases as { body: unknown }),
  });
  renderWithProviders(
    <Routes>
      <Route
        path="/inbox"
        element={
          <RequireAuth>
            <InboxPage />
          </RequireAuth>
        }
      />
    </Routes>,
    "/inbox",
  );
  return api;
}

const rowIds = () =>
  within(screen.getByRole("table"))
    .getAllByRole("row")
    .slice(1)
    .map((r) => within(r).getByRole("rowheader").textContent ?? "");

describe("InboxPage", () => {
  it("shows a skeleton, then the cases most urgent first", async () => {
    renderInbox();
    expect(await screen.findByRole("table")).toBeInTheDocument();
    const ids = rowIds();
    // Needs a person first (P1 NEEDS_INFO, then P2 AWAITING_APPROVAL), then the rest by priority.
    expect(ids[0]).toContain("CASE-3");
    expect(ids[1]).toContain("CASE-2");
    expect(ids[2]).toContain("CASE-1");
    expect(ids[3]).toContain("CASE-4");
  });

  it("counts each view on its button", async () => {
    renderInbox();
    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: /^All 4/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Needs a person 2/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^In motion 1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Finished 1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^AI working 0/ })).toBeInTheDocument();
  });

  it("flags injection, degraded mode and duplicates in words", async () => {
    renderInbox();
    await screen.findByRole("table");
    expect(screen.getByText("Injection flagged")).toBeInTheDocument();
    expect(screen.getByText("Degraded")).toBeInTheDocument();
    expect(screen.getByText("Duplicate of CASE-2")).toBeInTheDocument();
  });

  it("a view filter narrows the table and marks the button pressed", async () => {
    renderInbox();
    await screen.findByRole("table");
    const btn = screen.getByRole("button", { name: /^Needs a person/ });
    await userEvent.click(btn);
    expect(btn).toHaveAttribute("aria-pressed", "true");
    expect(rowIds()).toHaveLength(2);
    expect(rowIds().join(" ")).not.toContain("CASE-1");
  });

  it("search narrows by title", async () => {
    renderInbox();
    await screen.findByRole("table");
    await userEvent.type(screen.getByLabelText("Search"), "shelf");
    expect(rowIds()).toHaveLength(1);
    expect(rowIds()[0]).toContain("CASE-2");
  });

  it("says nothing matches and Clear filters brings everything back", async () => {
    renderInbox();
    await screen.findByRole("table");
    await userEvent.type(screen.getByLabelText("Search"), "zzzz");
    expect(screen.getByText("Nothing matches")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(rowIds()).toHaveLength(4);
  });

  it("links each case to its page", async () => {
    renderInbox();
    await screen.findByRole("table");
    expect(screen.getByRole("link", { name: "Pune shelf shortage" })).toHaveAttribute("href", "/cases/CASE-2");
  });

  it("an empty system invites a requester to create the first request", async () => {
    renderInbox({ cases: { body: { cases: [] } } });
    expect(await screen.findByText("No requests yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create the first request" })).toHaveAttribute("href", "/new");
  });

  it("an empty system does not offer admin a create button", async () => {
    renderInbox({ cases: { body: { cases: [] } }, role: "admin" });
    expect(await screen.findByText("No requests yet")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /new request|create the first/i })).not.toBeInTheDocument();
  });

  it("shows the header New request link to everyone but admin", async () => {
    renderInbox();
    await screen.findByRole("table");
    expect(screen.getByRole("link", { name: "New request" })).toBeInTheDocument();
  });

  it("shows a clear error with a retry when the list fails", async () => {
    renderInbox({ cases: { status: 500, body: { error: "DB down", request_id: "req-8" } } });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load cases");
    expect(alert).toHaveTextContent("DB down");
    expect(alert).toHaveTextContent("req-8");
    expect(within(alert).getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});
