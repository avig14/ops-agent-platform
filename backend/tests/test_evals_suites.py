"""The eval suites themselves are code that can be wrong. These tests pin what each one must report, and the invariants of the
scoring (a model that says nothing must not score false positives; a missing recording must never look like a pass)."""
import json

import pytest

from app.evals import datasets as ds
from app.evals import run, suites
from app.evals.gateway import EvalGateway


# ── datasets ──
def test_gold_set_is_well_formed():
    cases, h = ds.load_extraction()
    assert len(cases) == 26 and len(h) == 12 and len({c["id"] for c in cases}) == 26
    from app import extract
    for c in cases:
        assert set(c["gold"]) == set(extract.FIELDS), c["id"]
        assert set(c["expect"]) == {"injection", "cancellation"}


def test_value_matching_rules():
    assert ds.value_matches("quantity_affected", "500", "five hundred")
    assert ds.value_matches("additional_cost", "150000", "INR 1.5 lakh")
    assert not ds.value_matches("quantity_affected", "500", "50")
    assert ds.value_matches("location", "Pune", "the Pune store")
    assert ds.value_matches("location", ["Pune", "Poona"], "poona")
    assert ds.value_matches("supplier", None, None) and not ds.value_matches("supplier", None, "Acme")
    assert [ds.classify("delay_days", g, v) for g, v in [(None, None), (None, "3"), ("3", None), ("3", "3"), ("3", "4")]] == ["tn", "fp", "fn", "tp", "wrong"]


def test_attack_set_shape_and_holdout_is_separate():
    a = ds.attack_set()
    assert len(a) == (len(ds.DEV_PAYLOADS) + len(ds.HOLDOUT_PAYLOADS)) * len(ds.WRAPPERS) == 112
    assert {x["split"] for x in a} == {"dev", "holdout"}
    assert not {p[0] for p in ds.DEV_PAYLOADS} & {p[0] for p in ds.HOLDOUT_PAYLOADS}


# ── scripted suites ──
async def test_injection_suite_reports_dev_clean_and_discloses_holdout():
    r = await suites.injection_suite()
    assert r["overall"]["attacks"] == 112 and r["dev"]["attacks"] == 64 and r["holdout"]["attacks"] == 48
    assert r["dev"]["attack_success_llm_path"]["n"] == 56                    # base64 attacks carry no readable poison, so 7 wrappers x 8
    assert r["dev"]["attack_success_llm_path"]["k"] == 0                     # tuned-on split: no poisoned value survives
    assert r["dev"]["attack_success_rules_path"]["k"] == 0
    assert r["holdout"]["attack_success_llm_path"]["n"] == 42
    assert r["holdout"]["detected"]["rate"] < 1.0                            # we report the miss, we do not hide it
    assert r["holdout"]["detected"]["ci95"][0] < r["holdout"]["detected"]["rate"] < r["holdout"]["detected"]["ci95"][1]
    assert set(r["by_wrapper"]) == set(ds.WRAPPERS) and set(r["by_payload"]) == {p[0] for p in ds.DEV_PAYLOADS + ds.HOLDOUT_PAYLOADS}


async def test_scanner_has_no_false_positives_on_benign_controls_or_clean_gold():
    r = await suites.false_positive_suite()
    assert r["checked"] == 38 and r["false_positives"]["k"] == 0, r["flagged"]


async def test_gates_all_pass_and_the_known_limitation_is_listed():
    r = await suites.gate_suite()
    assert r["passed"] == r["of"] >= 13
    assert r["limitations"] == ["G14_cross_issue_quote_borrowing"] or len(r["limitations"]) == 1


def test_policy_and_risk_and_metamorphic_have_no_failures():
    p, k = suites.policy_suite(), suites.risk_suite()
    assert p["failed"] == [] and p["checks"] >= 27
    assert k["failed"] == [] and k["checks"] >= 8
    m = suites.metamorphic_suite()
    assert m["failures"] == {}, m["failures"]


# ── extraction scoring ──
async def test_rules_only_extraction_is_scored_without_any_gateway():
    r = await suites.extraction_suite(None)
    assert list(r["modes"]) == ["rules_only"]                                # LLM modes are skipped, not faked
    m = r["modes"]["rules_only"]
    assert m["scored"] == 26 and m["not_scored"] == []
    assert m["overall"]["precision"] >= 0.95 and m["hallucination"]["k"] == 0
    assert m["overall"]["tp"] + m["overall"]["fn"] + m["overall"]["wrong"] > 0


async def test_missing_recording_is_reported_as_not_scored_never_as_a_pass(tmp_path):
    gw = EvalGateway(mode="replay", store=tmp_path / "none.json", models=("a", "b"))
    r = await suites.extraction_suite(gw, only=("llm",))
    m = r["modes"]["llm"]
    assert m["scored"] == 0 and len(m["not_scored"]) == 26 and m["overall"]["precision"] is None


async def test_a_model_that_says_nothing_makes_no_false_positives():
    gw = EvalGateway(mode="scripted", models=("a", "b"), transport=suites.Fixed(suites.intake_json({})), rpm=60000)
    r = await suites.extraction_suite(gw, only=("llm",))
    m = r["modes"]["llm"]
    assert m["scored"] == 26 and m["overall"]["fp"] == 0 and m["overall"]["tp"] == 0 and m["overall"]["recall"] == 0
    assert m["calibration"] == {"n": 0}


def test_calibration_bins_follow_the_product_bands():
    c = suites.calibration([(0.9, True), (0.9, True), (0.7, True), (0.7, False), (0.3, False)])
    assert [b["band"] for b in c["bins"]] == ["abstain", "confirm", "high"] and c["n"] == 5
    assert 0 <= c["brier"] <= 1


def test_cost_latency_handles_no_calls_and_failures():
    assert suites.cost_latency(None) == {"calls": 0}
    gw = EvalGateway(mode="scripted", models=("a",), transport=suites.Fixed({}))
    gw.calls = [{"model": "a", "status": 200, "ms": 100, "tokens_in": 10, "tokens_out": 2},
                {"model": "a", "status": 429, "ms": 5, "tokens_in": 0, "tokens_out": 0}]
    r = suites.cost_latency(gw)
    assert r["calls"] == 2 and r["ok"] == 1 and r["failed"] == 1 and r["tokens_in"] == 10 and r["latency_ms"]["p50"] == 100


# ── real-model probe ──
async def test_probe_without_recording_is_not_scored(tmp_path):
    gw = EvalGateway(mode="replay", store=tmp_path / "none.json", models=("a", "b"))
    r = await suites.llm_probe_suite(gw)
    assert r["attacks"] == 0 and len(r["not_scored"]) == 7


async def test_probe_counts_model_flags_the_scanner_missed():
    flagging = suites.intake_json({}, flag=True)
    gw = EvalGateway(mode="scripted", models=("a", "b"), transport=suites.Fixed(flagging), rpm=60000)
    r = await suites.llm_probe_suite(gw)
    assert r["attacks"] == 6 and r["flagged_by_any_layer"]["k"] == 6 and r["control_false_flag"] is True
    assert r["poison_survived"]["k"] == 0


# ── runner ──
async def test_runner_replay_makes_no_calls_and_stamps_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RECORDING", tmp_path / "rec.json")
    r = await run.run(live=False)
    p = r["provenance"]
    assert p["run_mode"] == "replay" and p["api_calls_this_run"] == 0 and p["prompt_hash"] == run.prompt_hash()
    assert p["git_sha"] is None and r["limits"] and r["real_model"]["extraction"]["modes"]["llm"]["scored"] == 0
    assert not (tmp_path / "rec.json").exists()                              # replay never writes a recording
    json.dumps(r, default=str)                                               # the snapshot is serialisable
    assert any("policy" in ln for ln in run.headline(r))
