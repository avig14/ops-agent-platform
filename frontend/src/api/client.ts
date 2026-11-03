/** One fetch wrapper for the whole app. Every failure becomes an ApiError that says what broke. */

export interface FieldIssue {
  loc: string[];
  msg: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly requestId: string | null;
  readonly issues: FieldIssue[];

  constructor(status: number, message: string, requestId: string | null = null, issues: FieldIssue[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
    this.issues = issues;
  }

  /** What the person should do next, in one short line. */
  get hint(): string {
    if (this.status === 0) return "Check that the server is running, then try again.";
    if (this.status === 401) return "Sign in again.";
    if (this.status === 403) return "Your role cannot do this. Ask someone with authority.";
    if (this.status === 404) return "It may have been removed, or you may not have access.";
    if (this.status === 409) return "The case changed while you were looking. Reload and try again.";
    if (this.status === 422) return "Fix the input and try again.";
    if (this.status === 429) return "Too many attempts. Wait a few minutes.";
    if (this.status >= 500) return "Server problem. Try again; if it repeats, quote the request id.";
    return "Try again.";
  }
}

interface Options {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: Options["query"]): string {
  if (!query) return path;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `${path}?${s}` : path;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const { method = "GET", body, query, signal } = opts;
  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      credentials: "same-origin",
      headers:
        body === undefined
          ? { Accept: "application/json" }
          : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "Cannot reach the server");
  }

  const headerId = res.headers.get("X-Request-ID");
  let data: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    const rec = isRecord(data) ? data : {};
    const message = typeof rec.error === "string" ? rec.error : `Request failed (${res.status})`;
    const requestId = typeof rec.request_id === "string" ? rec.request_id : headerId;
    const issues = Array.isArray(rec.detail)
      ? rec.detail.filter(isRecord).map((d) => ({
          loc: Array.isArray(d.loc) ? d.loc.map(String) : [],
          msg: typeof d.msg === "string" ? d.msg : "invalid",
        }))
      : [];
    throw new ApiError(res.status, message, requestId, issues);
  }
  return data as T;
}
