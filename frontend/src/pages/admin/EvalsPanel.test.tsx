import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EvalMode, EvalRate, EvalSnapshot } from "../../api/types";
import { mockApi, renderWithProviders } from "../../test/mockApi";
import { EvalsPanel } from "./EvalsPanel";

afterEach(() => vi.restoreAllMocks());

const rate = (k: number, n: number, lo = 0, hi = 1): EvalRate => ({ k, n, rate: n ? k / n : null, ci95: [lo, hi] });

const prf = { tp: 8, fp: 1, fn: 2, tn: 5, wrong: 1, precision: 0.89, recall: 0.8, f1: 0.84 };

function mode(scored: number): EvalMode {
  return {
    scored,
    not_scored: scored ? [] : [{ id: "E01", error: "not_recorded" }],
    per_field: { delay_days: prf },
    overall: prf,
    precision_ci95: [0.6, 0.97],
    recall_ci95: [0.5, 0.9],
    hallucination: rate(0, 30, 0, 0.11),
    wrong_value: rate(1, 9, 0.02, 0.43),
    injection_flag_accuracy: rate(26, 26),
    cancellation_accuracy: rate(26, 26),
    calibration: {
      n: 40,
      brier: 0.12,
      bins: [
        { band: "abstain", lo: 0, hi: 0.55, n: 5, correct: 1, mean_confidence: 0.4, accuracy: 0.2, ci95: [0.04, 0.62] },
        { band: "confirm", lo: 0.55, hi: 0.8, n: 10, correct: 7, mean_confidence: 0.7, accuracy: 0.7, ci95: [0.4, 0.89] },
        { band: "high", lo: 0.8, hi: 1, n: 25, correct: 24, mean_confidence: 0.9, accuracy: 0.96, ci95: [0.8, 0.99] },
      ],
    },
  };
}

function snapshot(over: Partial<EvalSnapshot> = {}, modes: Record<string, EvalMode> = { rules_only: mode(26), llm: mode(26), "llm+second": mode(26) }): EvalSnapshot {
  const group = (det: EvalRate) => ({ attacks: 48, detected: det, evaded: rate(48 - det.k, 48), attack_success_llm_path: rate(0, 42, 0, 0.08), attack_success_rules_path: rate(0, 42), utility_under_attack: rate(200, 240, 0.8, 0.9) });
  return {
    available: true,
    stale: false,
    stale_because: [],
    provenance: { generated_at: "2026-11-03T18:00:00+00:00", run_mode: "live", dataset_hash: "abc123abc123", prompt_hash: "def456def456", models_in_recording: ["gemini-3.5-flash"], recorded_responses: 60, api_calls_this_run: 60, git_sha: null },
    scripted: {
      injection: {
        overall: group(rate(75, 96)),
        dev: group(rate(48, 48, 0.93, 1)),
        holdout: group(rate(27, 48, 0.42, 0.69)),
        by_wrapper: {},
        by_payload: { H1: group(rate(8, 8)), H2: group(rate(0, 8, 0, 0.32)), H4: group(rate(2, 8)) },
      },
      false_positives: { checked: 38, false_positives: rate(0, 38, 0, 0.09) },
      gates: { cases: [], passed: 13, of: 13, limitations: ["G14_cross_issue_quote_borrowing"] },
      policy: { checks: 27, passed: 27, failed: [] },
      risk: { checks: 8, passed: 8, failed: [] },
      metamorphic: {},
    },
    real_model: {
      extraction: { cases: 26, modes },
      injection_probe: { available: true, attacks: 6, flagged_by_any_layer: rate(5, 6), llm_caught_what_scanner_missed: rate(2, 3), poison_survived: rate(0, 6), control_false_flag: false },
      cost_latency: { calls: 60, ok: 56, failed: 4, latency_ms: { p50: 9000, p95: 21000, max: 30000 }, tokens_per_call: { in: 230, out: 260 } },
    },
    limits: ["Small, synthetic test sets.", "Confidence scores are heuristics."],
    ...over,
  };
}

function show(body: unknown, status = 200) {
  mockApi({ "GET /api/admin/evals": { status, body } });
  return renderWithProviders(<EvalsPanel />);
}

describe("EvalsPanel", () => {
  it("says plainly when no measurements exist yet", async () => {
    show({ available: false, hint: "run" });
    expect(await screen.findByText(/No measurements yet/)).toBeInTheDocument();
  });

  it("shows the headline numbers with their counts", async () => {
    show(snapshot());
    expect(await screen.findByText("27 of 27")).toBeInTheDocument();
    expect(screen.getByText("13 of 13")).toBeInTheDocument();
    expect(screen.getByText("0 of 38")).toBeInTheDocument();
    const stat = screen.getByText("Unseen attacks caught").closest(".stat") as HTMLElement;
    expect(within(stat).getByText("56%")).toBeInTheDocument();
    expect(within(stat).getByText(/27 of 48/)).toBeInTheDocument();
  });

  it("shows unseen-attack misses in the open instead of hiding them behind the tuned score", async () => {
    show(snapshot());
    const note = await screen.findByText(/It misses:/);
    expect(note).toHaveTextContent("H2 (0% caught)");
    expect(note).toHaveTextContent("H4 (25% caught)");
    expect(note).not.toHaveTextContent("H1");
    expect(screen.getByText(/Real AI check on 6 unseen attacks/)).toHaveTextContent("5 of 6");
  });

  it("marks a method with no recorded answers as not measured, never as zero", async () => {
    show(snapshot({}, { rules_only: mode(26), llm: mode(0), "llm+second": mode(0) }));
    const table = await screen.findByRole("table", { name: "Extraction accuracy by method" });
    expect(within(table).getAllByText(/Not measured/)).toHaveLength(2);
    expect(within(table).getByText("26")).toBeInTheDocument();
  });

  it("flags a stale snapshot in words", async () => {
    show(snapshot({ stale: true, stale_because: ["prompt_hash"] }));
    expect(await screen.findByText("This snapshot is out of date")).toBeInTheDocument();
    expect(screen.getByText(/the AI instructions changed after this snapshot/)).toBeInTheDocument();
  });

  it("lists the limitations and where the numbers came from", async () => {
    show(snapshot());
    expect(await screen.findByText("Small, synthetic test sets.")).toBeInTheDocument();
    expect(screen.getByText("abc123abc123")).toBeInTheDocument();
    expect(screen.getByText(/gemini-3.5-flash \(60 recorded answers\)/)).toBeInTheDocument();
  });

  it("shows the confidence bands with the actual hit rate", async () => {
    show(snapshot());
    const table = await screen.findByRole("table", { name: "Confidence against actual accuracy" });
    expect(within(table).getByText("24 of 25, 96%")).toBeInTheDocument();
    expect(within(table).getByText("1 of 5, 20%")).toBeInTheDocument();
  });

  it("tells the viewer when the server refuses (non-admin)", async () => {
    show({ error: "forbidden" }, 403);
    expect(await screen.findByText(/load the quality measurements/i)).toBeInTheDocument();
  });
});
