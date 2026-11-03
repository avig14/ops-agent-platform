import { useId, useState, type KeyboardEvent } from "react";

interface Props {
  label: string;
  prompt: string;
  confirmLabel: string;
  onConfirm: (reason: string) => void;
  /** Ask for a written reason. The confirm button stays disabled until it is long enough. */
  reasonLabel?: string;
  minReason?: number;
  tone?: "primary" | "danger" | "plain";
  disabled?: boolean;
  pending?: boolean;
}

/** Two-step button: the first press opens an inline confirmation, the second one acts. No modal, so focus never gets trapped. */
export function Confirm({ label, prompt, confirmLabel, onConfirm, reasonLabel, minReason = 3, tone = "plain", disabled, pending }: Props) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const id = useId();
  const needsReason = reasonLabel !== undefined;
  const reasonOk = !needsReason || reason.trim().length >= minReason;

  function close() {
    setOpen(false);
    setReason("");
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") close();
  }

  if (!open) {
    return (
      <button type="button" className={`btn btn--${tone}`} disabled={disabled || pending} onClick={() => setOpen(true)}>
        {label}
      </button>
    );
  }

  return (
    <div className="confirm" role="group" aria-labelledby={`${id}-p`} onKeyDown={onKey}>
      <p id={`${id}-p`} className="confirm__prompt">
        {prompt}
      </p>
      {needsReason ? (
        <div className="field">
          <label htmlFor={`${id}-r`}>{reasonLabel}</label>
          <textarea id={`${id}-r`} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus maxLength={1000} />
        </div>
      ) : null}
      <div className="confirm__actions">
        <button
          type="button"
          className={`btn btn--${tone === "plain" ? "primary" : tone}`}
          disabled={!reasonOk || pending}
          autoFocus={!needsReason}
          onClick={() => {
            onConfirm(reason.trim());
            close();
          }}
        >
          {confirmLabel}
        </button>
        <button type="button" className="btn" onClick={close}>
          Cancel
        </button>
      </div>
    </div>
  );
}
