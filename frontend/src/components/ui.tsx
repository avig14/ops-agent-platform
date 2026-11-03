/** Small shared pieces. Every data region uses Boundary so loading, empty, error and full are always handled. */
import type { UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import type { ActorType } from "../api/types";
import { statusInfo, type Tone } from "../lib/labels";

export function Chip({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`chip chip--${tone}`} title={title}>
      {children}
    </span>
  );
}

export function StatusChip({ status }: { status: string }) {
  const s = statusInfo(status);
  return (
    <Chip tone={s.tone} title={s.hint}>
      {s.label}
    </Chip>
  );
}

const ACTOR: Record<ActorType, { label: string; tone: Tone; title: string }> = {
  human: { label: "Person", tone: "person", title: "A signed-in person did this" },
  agent: { label: "AI", tone: "instrument", title: "An AI agent did this" },
  system: { label: "System", tone: "neutral", title: "Platform code did this" },
};

export function ActorBadge({ type }: { type: ActorType }) {
  const a = ACTOR[type];
  return (
    <Chip tone={a.tone} title={a.title}>
      {a.label}
    </Chip>
  );
}

export function Skeleton({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="skeleton" role="status" aria-live="polite">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skeleton__row" aria-hidden="true" />
      ))}
    </div>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <p className="empty__title">{title}</p>
      {children ? <p className="empty__body">{children}</p> : null}
      {action ? <div className="empty__action">{action}</div> : null}
    </div>
  );
}

export function errorParts(error: unknown): { message: string; hint: string; requestId: string | null } {
  if (error instanceof ApiError) return { message: error.message, hint: error.hint, requestId: error.requestId };
  return { message: error instanceof Error ? error.message : "Something went wrong", hint: "Try again.", requestId: null };
}

export function ErrorBox({ error, what, onRetry }: { error: unknown; what: string; onRetry?: () => void }) {
  const e = errorParts(error);
  return (
    <div className="errorbox" role="alert">
      <p className="errorbox__title">Could not {what}</p>
      <p className="errorbox__body">
        {e.message}. {e.hint}
      </p>
      {e.requestId ? (
        <p className="errorbox__meta">
          Request id <span className="num">{e.requestId}</span>
        </p>
      ) : null}
      {onRetry ? (
        <button type="button" className="btn" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

/** Error line for a failed action (button press). Silent when nothing failed. */
export function ActionError({ error, what }: { error: unknown; what: string }) {
  if (!error) return null;
  const e = errorParts(error);
  return (
    <p className="action-error" role="alert">
      Could not {what}: {e.message}. {e.hint}
      {e.requestId ? (
        <>
          {" "}
          Request id <span className="num">{e.requestId}</span>
        </>
      ) : null}
    </p>
  );
}

interface BoundaryProps<T> {
  query: UseQueryResult<T>;
  what: string;
  skeleton?: ReactNode;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}

export function Boundary<T>({ query, what, skeleton, isEmpty, empty, children }: BoundaryProps<T>) {
  if (query.isPending) return <>{skeleton ?? <Skeleton label={`Loading ${what}`} />}</>;
  if (query.isError && query.data === undefined) return <ErrorBox error={query.error} what={`load ${what}`} onRetry={() => void query.refetch()} />;
  const data = query.data as T;
  if (isEmpty?.(data)) return <>{empty}</>;
  return <>{children(data)}</>;
}

export function PageHeader({ title, lead, actions }: { title: string; lead?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {lead ? <p className="page-header__lead">{lead}</p> : null}
      </div>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </div>
  );
}

export function Panel({ title, aside, children, id }: { title?: string; aside?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="panel" id={id} aria-labelledby={title && id ? `${id}-h` : undefined}>
      {title || aside ? (
        <header className="panel__head">
          {title ? <h2 id={id ? `${id}-h` : undefined}>{title}</h2> : <span />}
          {aside ? <div className="panel__aside">{aside}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}

export function Stat({ label, value, note, tone }: { label: string; value: ReactNode; note?: ReactNode; tone?: Tone }) {
  return (
    <div className={`stat${tone ? ` stat--${tone}` : ""}`}>
      <dt>{label}</dt>
      <dd className="num">{value}</dd>
      {note ? <dd className="stat__note">{note}</dd> : null}
    </div>
  );
}

/** Banner for a state the person must notice: degraded mode, injection flag, and so on. */
export function Notice({ tone = "neutral", title, children }: { tone?: Tone; title: string; children?: ReactNode }) {
  return (
    <div className={`notice notice--${tone}`} role="note">
      <p className="notice__title">{title}</p>
      {children ? <div className="notice__body">{children}</div> : null}
    </div>
  );
}
