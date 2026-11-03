import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Recommendation } from "../../api/types";
import { caseHead, detail, recommendation, user } from "../../test/factories";
import { mockApi, renderWithProviders } from "../../test/mockApi";
import { PlanTab } from "./PlanTab";

afterEach(() => vi.restoreAllMocks());

function plan(evidence: Recommendation["evidence"]) {
  mockApi({});
  const d = detail({
    case: caseHead({ status: "APPROVED" }),
    recommendations: [recommendation({ rationale: "Expedite from the alternate supplier.", chosen_option: "expedite_alternate", evidence })],
  });
  renderWithProviders(<PlanTab detail={d} user={user()} />);
}

describe("PlanTab checks on the AI's output", () => {
  it("shows a rejected check in plain words, with the code, and says a template wrote the plan", () => {
    plan({ rationale_source: "template", gate_events: ["GATE_COMMS_NUMBER"] });
    expect(screen.getByText("Rejected")).toBeInTheDocument();
    expect(screen.getByText(/not in the case facts/i)).toBeInTheDocument();
    expect(screen.getByText("GATE_COMMS_NUMBER")).toBeInTheDocument();
    expect(screen.getByText("Template wrote this")).toBeInTheDocument();
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
  });

  it("calls an outage a fallback, not a failed check", () => {
    plan({ rationale_source: "template", gate_events: ["llm_unavailable"] });
    expect(screen.getByText("Fallback")).toBeInTheDocument();
    expect(screen.queryByText("Rejected")).not.toBeInTheDocument();
  });

  it("says no check rejected an AI-written plan", () => {
    plan({ rationale_source: "llm", gate_events: [] });
    expect(screen.getByText("AI wrote this")).toBeInTheDocument();
    expect(screen.getByText(/no check rejected the ai's wording/i)).toBeInTheDocument();
  });

  it("hides the panel when the AI was not involved and nothing was rejected", () => {
    plan({ rationale_source: "template", gate_events: [] });
    expect(screen.queryByText("Checks on the AI's output")).not.toBeInTheDocument();
  });
});
