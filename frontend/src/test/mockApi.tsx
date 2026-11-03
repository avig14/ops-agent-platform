/** A fake server for component tests: fetch is replaced, routes are plain functions, calls are recorded. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { vi } from "vitest";

export interface Reply {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}

export type Handler = Reply | ((req: { url: string; method: string; body: unknown }) => Reply);

export interface Call {
  method: string;
  url: string;
  body: unknown;
}

/** Keys look like "GET /api/cases". The query string is ignored when matching. Unknown routes answer 404 so a missing mock is loud. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.pathname + input.search : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
    calls.push({ method, url, body });
    const handler = routes[`${method} ${url.split("?")[0]}`];
    const reply = handler === undefined ? { status: 404, body: { error: `no mock for ${method} ${url}` } } : typeof handler === "function" ? handler({ url, method, body }) : handler;
    const status = reply.status ?? 200;
    const text = reply.body === undefined ? "" : JSON.stringify(reply.body);
    return Promise.resolve(new Response(status === 204 ? null : text, { status, headers: reply.headers }));
  });
  return { calls, spy };
}

export function renderWithProviders(ui: ReactElement, route = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0, refetchInterval: false }, mutations: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, client };
}
