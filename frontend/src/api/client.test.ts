import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./client";

function reply(status: number, body?: unknown, headers?: Record<string, string>): Response {
  return new Response(body === undefined ? null : typeof body === "string" ? body : JSON.stringify(body), { status, headers });
}

function stubFetch(r: Response | Error | DOMException) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(() => (r instanceof Response ? Promise.resolve(r) : Promise.reject(r)));
}

afterEach(() => vi.restoreAllMocks());

describe("api success", () => {
  it("returns parsed JSON", async () => {
    stubFetch(reply(200, { cases: [1, 2] }));
    await expect(api("/api/cases")).resolves.toEqual({ cases: [1, 2] });
  });

  it("sends a GET with Accept and the session cookie, no body or content type", async () => {
    const spy = stubFetch(reply(200, {}));
    await api("/api/cases");
    const [url, init] = spy.mock.calls[0]!;
    expect(url).toBe("/api/cases");
    expect(init?.method).toBe("GET");
    expect(init?.credentials).toBe("same-origin");
    expect(init?.body).toBeUndefined();
    expect(init?.headers).toEqual({ Accept: "application/json" });
  });

  it("sends JSON bodies with a content type", async () => {
    const spy = stubFetch(reply(200, { ok: true }));
    await api("/api/auth/login", { method: "POST", body: { email: "a@b.c", password: "x" } });
    const init = spy.mock.calls[0]![1]!;
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"email":"a@b.c","password":"x"}');
    expect(init.headers).toEqual({ Accept: "application/json", "Content-Type": "application/json" });
  });

  it("builds a query string and skips empty values", async () => {
    const spy = stubFetch(reply(200, {}));
    await api("/api/cases", { query: { status: "NEEDS_INFO", q: "", a: null, b: undefined, n: 0, f: false } });
    expect(spy.mock.calls[0]![0]).toBe("/api/cases?status=NEEDS_INFO&n=0&f=false");
  });

  it("leaves the path alone when every query value is empty", async () => {
    const spy = stubFetch(reply(200, {}));
    await api("/api/cases", { query: { status: undefined } });
    expect(spy.mock.calls[0]![0]).toBe("/api/cases");
  });

  it("encodes query values", async () => {
    const spy = stubFetch(reply(200, {}));
    await api("/api/cases", { query: { q: "a&b=c d" } });
    expect(spy.mock.calls[0]![0]).toBe("/api/cases?q=a%26b%3Dc+d");
  });

  it("returns null for an empty body", async () => {
    stubFetch(reply(204));
    await expect(api("/api/x", { method: "DELETE" })).resolves.toBeNull();
  });
});

describe("api errors", () => {
  it("reads the server's {error, request_id} body", async () => {
    stubFetch(reply(409, { error: "Case changed", request_id: "req-1" }));
    const err = await api("/api/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(409);
    expect(e.message).toBe("Case changed");
    expect(e.requestId).toBe("req-1");
    expect(e.issues).toEqual([]);
  });

  it("keeps 422 field issues", async () => {
    stubFetch(reply(422, { error: "Invalid input", request_id: "r", detail: [{ loc: ["body", "text"], msg: "too short" }, { loc: [], msg: 5 }, "junk"] }));
    const e = (await api("/api/x").catch((x: unknown) => x)) as ApiError;
    expect(e.issues).toEqual([
      { loc: ["body", "text"], msg: "too short" },
      { loc: [], msg: "invalid" },
    ]);
  });

  it("falls back to the X-Request-ID header when the body is not ours", async () => {
    stubFetch(reply(502, "<html>Bad gateway</html>", { "X-Request-ID": "hdr-7" }));
    const e = (await api("/api/x").catch((x: unknown) => x)) as ApiError;
    expect(e.status).toBe(502);
    expect(e.message).toBe("Request failed (502)");
    expect(e.requestId).toBe("hdr-7");
  });

  it("has no request id when nothing supplies one", async () => {
    stubFetch(reply(500));
    const e = (await api("/api/x").catch((x: unknown) => x)) as ApiError;
    expect(e.requestId).toBeNull();
  });

  it("turns a network failure into status 0, not a raw TypeError", async () => {
    stubFetch(new TypeError("Failed to fetch"));
    const e = (await api("/api/x").catch((x: unknown) => x)) as ApiError;
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(0);
    expect(e.message).toBe("Cannot reach the server");
  });

  it("lets an abort through untouched so callers can ignore it", async () => {
    stubFetch(new DOMException("aborted", "AbortError"));
    const e = await api("/api/x").catch((x: unknown) => x);
    expect(e).toBeInstanceOf(DOMException);
    expect((e as DOMException).name).toBe("AbortError");
  });

  it("does not leak a non-string error field", async () => {
    stubFetch(reply(400, { error: { nested: true } }));
    const e = (await api("/api/x").catch((x: unknown) => x)) as ApiError;
    expect(e.message).toBe("Request failed (400)");
  });
});

describe("ApiError.hint", () => {
  const hint = (s: number) => new ApiError(s, "m").hint;
  it("tells the person what to do for each status", () => {
    expect(hint(0)).toMatch(/server is running/i);
    expect(hint(401)).toMatch(/sign in/i);
    expect(hint(403)).toMatch(/role/i);
    expect(hint(404)).toMatch(/removed|access/i);
    expect(hint(409)).toMatch(/reload/i);
    expect(hint(422)).toMatch(/fix the input/i);
    expect(hint(429)).toMatch(/wait/i);
    expect(hint(500)).toMatch(/request id/i);
    expect(hint(503)).toMatch(/request id/i);
    expect(hint(418)).toBe("Try again.");
  });
});
