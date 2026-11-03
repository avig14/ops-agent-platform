import { useState, type FormEvent } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { useDemoUsers, useLogin, useMe } from "../api/hooks";
import { Boundary, ActionError, Chip, Empty, Skeleton } from "../components/ui";
import { safeNext } from "../components/Auth";
import { roleLabel } from "../lib/labels";

const WHAT_ROLE_DOES: Record<string, string> = {
  requester: "Sends requests, answers questions, sees only their own region.",
  manager: "Approves up to INR 50,000 in their region.",
  regional_head: "Approves up to INR 5,00,000 in their region.",
  finance_director: "Co-approves above INR 5,00,000 and records spend.",
  admin: "Runs the platform: AI circuits, fault injection, audit check. Cannot approve.",
};

export function LoginPage() {
  const me = useMe();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const navigate = useNavigate();
  const demo = useDemoUsers();
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  if (me.data) return <Navigate to={next} replace />;

  function signIn(e: string, p: string) {
    login.mutate({ email: e, password: p }, { onSuccess: () => navigate(next, { replace: true }) });
  }

  function submit(ev: FormEvent) {
    ev.preventDefault();
    signIn(email.trim(), password);
  }

  return (
    <main className="login">
      <div className="login__intro">
        <h1>Ops Agent Console</h1>
        <p>
          AI reads a supply request, scores the risk and drafts a plan. People approve, override and close it. Every step is written to a
          tamper-evident audit trail.
        </p>
        <form className="login__form" onSubmit={submit} aria-label="Sign in with email">
          <div className="field">
            <label htmlFor="email">Email</label>
            <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button type="submit" className="btn btn--primary" disabled={login.isPending}>
            {login.isPending ? "Signing in" : "Sign in"}
          </button>
          <ActionError error={login.error} what="sign in" />
        </form>
      </div>

      <section className="login__demo" aria-labelledby="demo-h">
        <h2 id="demo-h">Demo accounts</h2>
        <p className="muted">One click signs you in. Try the same case as different people to see who can do what.</p>
        <Boundary
          query={demo}
          what="demo accounts"
          skeleton={<Skeleton rows={7} label="Loading demo accounts" />}
          isEmpty={(d) => d.users.length === 0}
          empty={<Empty title="No demo accounts">The server has no seeded users. Run the seed command, then reload.</Empty>}
        >
          {(d) => (
            <table className="table table--stack">
              <caption className="visually-hidden">Demo accounts</caption>
              <thead>
                <tr>
                  <th scope="col">Person</th>
                  <th scope="col">Role</th>
                  <th scope="col">What they can do</th>
                  <th scope="col">
                    <span className="visually-hidden">Sign in</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {d.users.map((u) => (
                  <tr key={u.email}>
                    <th scope="row">
                      {u.name}
                      <span className="table__sub">{u.email}</span>
                    </th>
                    <td>
                      <Chip tone="person">
                        {roleLabel(u.role)}
                        {u.region ? ` ${u.region}` : ""}
                      </Chip>
                    </td>
                    <td className="muted">{WHAT_ROLE_DOES[u.role] ?? ""}</td>
                    <td className="table__act">
                      <button type="button" className="btn" disabled={login.isPending} onClick={() => signIn(u.email, d.password)}>
                        Sign in as {u.name.replace(/\s*\(.*\)$/, "")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Boundary>
      </section>
    </main>
  );
}
