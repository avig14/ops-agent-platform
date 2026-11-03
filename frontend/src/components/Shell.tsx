import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { useLogout, useUnreadCount } from "../api/hooks";
import type { Role } from "../api/types";
import { roleLabel } from "../lib/labels";
import { useUser } from "./Auth";
import { ErrorBoundary } from "./ErrorBoundary";
import { Chip } from "./ui";

interface NavItem {
  to: string;
  label: string;
  roles?: readonly Role[];
  /** Also counts as active on these path prefixes. */
  also?: string[];
}

const NAV: NavItem[] = [
  { to: "/inbox", label: "Inbox", also: ["/cases"] },
  { to: "/new", label: "New request", roles: ["requester", "manager", "regional_head", "finance_director"] },
  { to: "/mailbox", label: "Mailbox" },
  { to: "/dashboard", label: "Dashboard", roles: ["manager", "regional_head", "finance_director", "admin"] },
  { to: "/how-it-works", label: "How it works" },
  { to: "/admin", label: "Admin", roles: ["admin"] },
];

export function Shell() {
  const user = useUser();
  const loc = useLocation();
  const navigate = useNavigate();
  const logout = useLogout();
  const unread = useUnreadCount(true);
  const items = NAV.filter((n) => !n.roles || n.roles.includes(user.role));

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <Link to="/inbox" className="brand">
          Ops Agent Console
        </Link>
        <nav className="topbar__nav" aria-label="Main">
          {items.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              className={({ isActive }) =>
                `navlink${isActive || n.also?.some((p) => loc.pathname.startsWith(p)) ? " navlink--active" : ""}`
              }
            >
              {n.label}
              {n.to === "/mailbox" && (unread.data ?? 0) > 0 ? (
                <span className="navlink__count num" aria-label={`${unread.data} unread`}>
                  {unread.data}
                </span>
              ) : null}
            </NavLink>
          ))}
        </nav>
        <div className="topbar__user">
          <span className="topbar__name">{user.name}</span>
          <Chip tone="person" title={user.region ? `Region: ${user.region}` : "All regions"}>
            {roleLabel(user.role)}
            {user.region ? ` ${user.region}` : ""}
          </Chip>
          <button
            type="button"
            className="btn btn--quiet"
            disabled={logout.isPending}
            onClick={() => logout.mutate(undefined, { onSettled: () => navigate("/login", { replace: true }) })}
          >
            Sign out
          </button>
        </div>
      </header>
      <main id="main" tabIndex={-1}>
        <ErrorBoundary key={loc.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </>
  );
}
