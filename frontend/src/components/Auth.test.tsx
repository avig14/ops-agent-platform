import { screen } from "@testing-library/react";
import { Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, renderWithProviders } from "../test/mockApi";
import { user } from "../test/factories";
import { RequireAuth, RequireRole, safeNext, useUser } from "./Auth";

afterEach(() => vi.restoreAllMocks());

describe("safeNext", () => {
  it("allows same-site paths, with query and hash", () => {
    expect(safeNext("/cases/CASE-1")).toBe("/cases/CASE-1");
    expect(safeNext("/inbox?view=attention&q=pune")).toBe("/inbox?view=attention&q=pune");
  });
  it("blocks open redirects", () => {
    expect(safeNext("//evil.test")).toBe("/inbox");
    expect(safeNext("/\\evil.test")).toBe("/inbox");
    expect(safeNext("https://evil.test")).toBe("/inbox");
    expect(safeNext("javascript:alert(1)")).toBe("/inbox");
    expect(safeNext("evil.test/path")).toBe("/inbox");
  });
  it("never loops back to the login page", () => {
    expect(safeNext("/login")).toBe("/inbox");
    expect(safeNext("/login?next=/x")).toBe("/inbox");
  });
  it("defaults for empty input", () => {
    expect(safeNext(null)).toBe("/inbox");
    expect(safeNext(undefined)).toBe("/inbox");
    expect(safeNext("")).toBe("/inbox");
  });
});

function Where() {
  const l = useLocation();
  return <p>at {l.pathname + l.search}</p>;
}

function Tree() {
  return (
    <Routes>
      <Route path="/login" element={<Where />} />
      <Route
        path="/private"
        element={
          <RequireAuth>
            <Where />
          </RequireAuth>
        }
      />
      <Route
        path="/admin"
        element={
          <RequireAuth>
            <RequireRole roles={["admin"]} />
          </RequireAuth>
        }
      >
        <Route index element={<p>secret admin page</p>} />
      </Route>
    </Routes>
  );
}

function WhoAmI() {
  return <p>hello {useUser().name}</p>;
}

describe("RequireAuth", () => {
  it("shows a loading state first", () => {
    mockApi({ "GET /api/auth/me": () => ({ body: user() }) });
    renderWithProviders(<Tree />, "/private");
    expect(screen.getByRole("status")).toHaveTextContent(/checking your session/i);
  });

  it("sends a signed-out visitor to login and remembers where they were going", async () => {
    mockApi({ "GET /api/auth/me": { status: 401, body: { error: "Not signed in", request_id: "r" } } });
    renderWithProviders(<Tree />, "/private?x=1");
    expect(await screen.findByText("at /login?next=%2Fprivate%3Fx%3D1")).toBeInTheDocument();
  });

  it("renders the page for a signed-in user", async () => {
    mockApi({ "GET /api/auth/me": { body: user() } });
    renderWithProviders(<Tree />, "/private");
    expect(await screen.findByText("at /private")).toBeInTheDocument();
  });

  it("shows an error with a retry when the session check itself fails", async () => {
    mockApi({ "GET /api/auth/me": { status: 500, body: { error: "boom", request_id: "req-9" } } });
    renderWithProviders(<Tree />, "/private");
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not check your session/i);
    expect(screen.getByText("req-9")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("gives children the user through useUser", async () => {
    mockApi({ "GET /api/auth/me": { body: user({ name: "Priya Nair" }) } });
    renderWithProviders(
      <RequireAuth>
        <WhoAmI />
      </RequireAuth>,
    );
    expect(await screen.findByText("hello Priya Nair")).toBeInTheDocument();
  });
});

describe("RequireRole", () => {
  it("explains instead of showing a blank page", async () => {
    mockApi({ "GET /api/auth/me": { body: user({ role: "requester" }) } });
    renderWithProviders(<Tree />, "/admin");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/not available for your role/i);
    expect(alert).toHaveTextContent(/signed in as requester/i);
    expect(screen.queryByText("secret admin page")).not.toBeInTheDocument();
  });

  it("lets the right role through", async () => {
    mockApi({ "GET /api/auth/me": { body: user({ role: "admin", region: null }) } });
    renderWithProviders(<Tree />, "/admin");
    expect(await screen.findByText("secret admin page")).toBeInTheDocument();
  });
});
