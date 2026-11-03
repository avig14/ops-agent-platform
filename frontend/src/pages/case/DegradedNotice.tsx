import { useRetry } from "../../api/hooks";
import type { CaseDetail, User } from "../../api/types";
import { ActionError, Notice } from "../../components/ui";
import { humanize } from "../../lib/format";
import { canRetryAnalysis } from "../../lib/permissions";

/**
 * "Analysis is incomplete" banner plus the retry button, and the answer to the last click.
 * The answer is kept here, not inside the banner, because a successful retry clears the degraded flag
 * and the banner goes away, but the person still needs to hear what happened.
 */
export function DegradedNotice({ detail, user }: { detail: CaseDetail; user: User }) {
  const c = detail.case;
  const retry = useRetry(c.id);
  const result = retry.data;
  const canRetry = canRetryAnalysis(user, detail);

  return (
    <>
      {c.degraded ? (
        <Notice tone="person" title="Analysis is incomplete">
          A dependency was down (the AI model or a data source). No spend is recommended and a person must review. The Audit tab lists exactly what could not be fetched.
          {canRetry ? (
            <p>
              <button type="button" className="btn" disabled={retry.isPending} onClick={() => retry.mutate()}>
                {retry.isPending ? "Retrying" : "Retry the analysis"}
              </button>
            </p>
          ) : null}
          {result?.still_degraded ? (
            <p role="status">
              The AI is still unavailable, so nothing changed. The case keeps its place and its deadline.
              {result.failures && result.failures.length > 0 ? ` Last failure: ${result.failures.map(humanize).join(", ")}.` : ""} Try again in a minute.
            </p>
          ) : null}
          <ActionError error={retry.error} what="retry the analysis" />
        </Notice>
      ) : null}
      {!c.degraded && result?.recovered ? (
        <Notice tone="ok" title="Analysis recovered">
          <p role="status">
            The AI read the request again and the plan was rebuilt. Approval requests were raised again on the new plan, and the approval deadline did not move.
            {result.kept_human_values && result.kept_human_values.length > 0
              ? ` Values a person had already set were kept: ${result.kept_human_values.map(humanize).join(", ")}.`
              : ""}
          </p>
        </Notice>
      ) : null}
    </>
  );
}
