"""The eval report is only as honest as its arithmetic. Every expected value here was computed by hand."""
import pytest

from app.evals import stats


def test_wilson_matches_hand_computed_values():
    lo, hi = stats.wilson(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    lo, hi = stats.wilson(0, 10)
    assert lo == 0.0 and round(hi, 4) == 0.2775
    lo, hi = stats.wilson(10, 10)
    assert round(lo, 4) == 0.7225 and hi == pytest.approx(1.0)      # float noise at the edge is fine


def test_wilson_with_no_data_says_we_know_nothing():
    assert stats.wilson(0, 0) == (0.0, 1.0)


def test_rule_of_three():
    assert stats.rule_of_three(50) == pytest.approx(0.06)
    assert stats.rule_of_three(2) == 1.0          # capped: a rate cannot exceed 1
    assert stats.rule_of_three(0) is None


def test_rate_bundles_count_rate_and_interval():
    r = stats.rate(5, 10)
    assert r["k"] == 5 and r["n"] == 10 and r["rate"] == 0.5 and r["ci95"] == [0.2366, 0.7634]
    assert stats.rate(0, 0)["rate"] is None


def test_prf_basic_and_undefined_cases():
    r = stats.prf(8, 2, 2)
    assert r["precision"] == 0.8 and r["recall"] == 0.8 and r["f1"] == pytest.approx(0.8)
    none = stats.prf(0, 0, 0)
    assert none["precision"] is None and none["recall"] is None and none["f1"] is None      # not 0, not 1: undefined
    only_fn = stats.prf(0, 0, 3)
    assert only_fn["precision"] is None and only_fn["recall"] == 0.0 and only_fn["f1"] is None
    all_wrong = stats.prf(0, 2, 2)
    assert all_wrong["precision"] == 0.0 and all_wrong["recall"] == 0.0 and all_wrong["f1"] is None


def test_brier():
    assert stats.brier([(0.9, True), (0.8, False)]) == pytest.approx(0.325)
    assert stats.brier([(1.0, True)]) == 0.0
    assert stats.brier([]) is None


def test_reliability_bins_include_upper_edge_only_in_the_last_bin():
    pairs = [(0.2, False), (0.4, True), (0.5, True), (1.0, True)]
    low, high = stats.reliability(pairs, [0.0, 0.5, 1.0])
    assert (low["n"], low["correct"], low["accuracy"]) == (2, 1, 0.5)
    assert (high["n"], high["correct"], high["accuracy"]) == (2, 2, 1.0)      # 0.5 lands in the upper bin, 1.0 is included
    assert low["mean_confidence"] == pytest.approx(0.3)


def test_reliability_empty_bin_is_reported_not_dropped():
    (only,) = stats.reliability([], [0.0, 1.0])
    assert only["n"] == 0 and only["accuracy"] is None and only["ci95"] is None


def test_risk_coverage():
    pairs = [(0.2, False), (0.4, True), (0.5, True), (1.0, True)]
    a, b = stats.risk_coverage(pairs, [0.0, 0.5])
    assert a["kept"] == 4 and a["coverage"] == 1.0 and a["precision"] == 0.75
    assert b["kept"] == 2 and b["coverage"] == 0.5 and b["precision"] == 1.0
    (none,) = stats.risk_coverage(pairs, [2.0])
    assert none["kept"] == 0 and none["precision"] is None


def test_percentile_nearest_rank():
    v = list(range(1, 11))
    assert stats.percentile(v, 50) == 5
    assert stats.percentile(v, 95) == 10
    assert stats.percentile([7], 95) == 7
    assert stats.percentile([], 50) is None
