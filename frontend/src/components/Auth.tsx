import { createContext, useContext, type ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router";
import { useMe } from "../api/hooks";
import type { Role, User } from "../api/types";
import { ErrorBox, Skeleton } from "./ui";

const UserContext = createContext<User | null>(null);

export function useUser(): User {
  const u = useContext(UserContext);
  if (!u) throw new Error("useUser must be used inside RequireAuth");
  return u;
}

/** Only same-site relative paths may be used as a post-login target. Blocks `//evil.test` and `https://...`. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/inbox";
  if (next.startsWith("/login")) return "/inbox";
  return next;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();
  const loc = useLocation();
  if (me.isPending) {
    return (
      <main className="page page--narrow">
        <Skeleton rows={4} label="Checking your session" />
      </main>
    );
  }
  if (me.isError) {
    return (
      <main className="page page--narrow">
        <ErrorBox error={me.error} what="check your session" onRetry={() => void me.refetch()} />
      </main>
    );
  }
  if (!me.data) {
    const next = `${loc.pathname}${loc.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <UserContext.Provider value={me.data}>{children}</UserContext.Provider>;
}

/** Route guard by role. Wrong role gets a plain explanation, not a blank page. */
export function RequireRole({ roles }: { roles: readonly Role[] }) {
  const user = useUser();
  if (!roles.includes(user.role)) {
    return (
      <div className="page page--narrow">
        <div className="errorbox" role="alert">
          <p className="errorbox__title">Not available for your role</p>
          <p className="errorbox__body">This page is for {roles.join(", ").replace(/_/g, " ")}. You are signed in as {user.role.replace(/_/g, " ")}.</p>
        </div>
      </div>
    );
  }
  return <Outlet />;
}
