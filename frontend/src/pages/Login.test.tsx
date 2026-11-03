import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { user as makeUser } from "../test/factories";
import { mockApi, renderWithProviders } from "../test/mockApi";
import { LoginPage } from "./Login";

afterEach(() => vi.restoreAllMocks());

const DEMO = {
  password: "Demo@2026",
  users: [
    { email: "priya.requester@demo.test", name: "Priya Nair", role: "requester", region: "West" },
    { email: "anita.regional@demo.test", name: "Anita Rao", role: "regional_head", region: "West" },
    { email: "admin@demo.test", name: "Admin User", role: "admin", region: null },
  ],
};

const signedOut = { status: 401, body: { error: "Not signed in", request_id: "r" } };

function page() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/inbox" element={<p>inbox page</p>} />
      <Route path="/cases/:id" element={<p>case page</p>} />
    </Routes>
  );
}

describe("LoginPage", () => {
  it("lists every demo account with what that role can do", async () => {
    mockApi({ "GET /api/auth/me": signedOut, "GET /api/auth/demo-users": { body: DEMO } });
    renderWithProviders(page(), "/login");
    expect(await screen.findByRole("button", { name: "Sign in as Priya Nair" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in as Anita Rao" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in as Admin User" })).toBeInTheDocument();
    expect(screen.getByText(/cannot approve/i)).toBeInTheDocument();
    expect(screen.getByText("anita.regional@demo.test")).toBeInTheDocument();
  });

  it("one click signs in with the demo password and lands on the inbox", async () => {
    const { calls } = mockApi({
      "GET /api/auth/me": signedOut,
      "GET /api/auth/demo-users": { body: DEMO },
      "POST /api/auth/login": { body: makeUser({ role: "regional_head" }) },
    });
    renderWithProviders(page(), "/login");
    await userEvent.click(await screen.findByRole("button", { name: "Sign in as Anita Rao" }));
    expect(await screen.findByText("inbox page")).toBeInTheDocument();
    const login = calls.find((c) => c.method === "POST");
    expect(login?.body).toEqual({ email: "anita.regional@demo.test", password: "Demo@2026" });
  });

  it("signs in by form, trimming the email", async () => {
    const { calls } = mockApi({
      "GET /api/auth/me": signedOut,
      "GET /api/auth/demo-users": { body: DEMO },
      "POST /api/auth/login": { body: makeUser() },
    });
    renderWithProviders(page(), "/login");
    await userEvent.type(await screen.findByLabelText("Email"), "  priya.requester@demo.test ");
    await userEvent.type(screen.getByLabelText("Password"), "Demo@2026");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("inbox page")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ email: "priya.requester@demo.test", password: "Demo@2026" });
  });

  it("goes back to the page the person was heading to", async () => {
    mockApi({
      "GET /api/auth/me": signedOut,
      "GET /api/auth/demo-users": { body: DEMO },
      "POST /api/auth/login": { body: makeUser() },
    });
    renderWithProviders(page(), "/login?next=%2Fcases%2FCASE-7");
    await userEvent.click(await screen.findByRole("button", { name: "Sign in as Priya Nair" }));
    expect(await screen.findByText("case page")).toBeInTheDocument();
  });

  it("ignores a hostile next target", async () => {
    mockApi({
      "GET /api/auth/me": signedOut,
      "GET /api/auth/demo-users": { body: DEMO },
      "POST /api/auth/login": { body: makeUser() },
    });
    renderWithProviders(page(), "/login?next=%2F%2Fevil.test");
    await userEvent.click(await screen.findByRole("button", { name: "Sign in as Priya Nair" }));
    expect(await screen.findByText("inbox page")).toBeInTheDocument();
  });

  it("shows why a sign-in failed, with the request id", async () => {
    mockApi({
      "GET /api/auth/me": signedOut,
      "GET /api/auth/demo-users": { body: DEMO },
      "POST /api/auth/login": { status: 401, body: { error: "Wrong email or password", request_id: "req-42" } },
    });
    renderWithProviders(page(), "/login");
    await userEvent.type(await screen.findByLabelText("Email"), "x@y.z");
    await userEvent.type(screen.getByLabelText("Password"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not sign in: Wrong email or password.");
    expect(alert).toHaveTextContent("req-42");
    expect(screen.queryByText("inbox page")).not.toBeInTheDocument();
  });

  it("tells a rate-limited person to wait", async () => {
    mockApi({
      "GET /api/auth/me": signedOut,
      "GET /api/auth/demo-users": { body: DEMO },
      "POST /api/auth/login": { status: 429, body: { error: "Too many attempts", request_id: "r" } },
    });
    renderWithProviders(page(), "/login");
    await userEvent.click(await screen.findByRole("button", { name: "Sign in as Priya Nair" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/wait a few minutes/i);
  });

  it("skips the page when already signed in", async () => {
    mockApi({ "GET /api/auth/me": { body: makeUser() }, "GET /api/auth/demo-users": { body: DEMO } });
    renderWithProviders(page(), "/login");
    expect(await screen.findByText("inbox page")).toBeInTheDocument();
  });

  it("keeps the email form usable when the demo list fails to load", async () => {
    mockApi({ "GET /api/auth/me": signedOut, "GET /api/auth/demo-users": { status: 503, body: { error: "Down", request_id: "r" } } });
    renderWithProviders(page(), "/login");
    expect(await screen.findByText(/could not load demo accounts/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
  });

  it("explains an empty demo list", async () => {
    mockApi({ "GET /api/auth/me": signedOut, "GET /api/auth/demo-users": { body: { password: "x", users: [] } } });
    renderWithProviders(page(), "/login");
    expect(await screen.findByText("No demo accounts")).toBeInTheDocument();
  });

  it("disables the buttons while a sign-in is in flight", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/auth/login")) {
        await gate;
        return new Response(JSON.stringify(makeUser()), { status: 200 });
      }
      if (url.endsWith("/auth/demo-users")) return new Response(JSON.stringify(DEMO), { status: 200 });
      void init;
      return new Response(JSON.stringify({ error: "no" }), { status: 401 });
    });
    renderWithProviders(page(), "/login");
    await userEvent.click(await screen.findByRole("button", { name: "Sign in as Priya Nair" }));
    const form = screen.getByRole("form", { name: /sign in with email/i });
    await waitFor(() => expect(within(form).getByRole("button", { name: "Signing in" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Sign in as Anita Rao" })).toBeDisabled();
    release();
    expect(await screen.findByText("inbox page")).toBeInTheDocument();
  });
});
