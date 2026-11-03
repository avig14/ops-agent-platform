"""Small, dependency-free statistics for the eval report. Every function is unit-tested against hand-computed values."""
from __future__ import annotations

import math
from typing import Iterable, Sequence

Z95 = 1.959964  # two-sided 95%


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n. Safe for k=0, k=n. n=0 gives (0, 1): we know nothing."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rule_of_three(n: int) -> float | None:
    """Approximate 95% upper bound on a rate when 0 events were seen in n trials: 3/n. None when n=0."""
    return None if n <= 0 else min(1.0, 3.0 / n)


def rate(k: int, n: int) -> dict:
    """Count, rate and Wilson interval together, in the shape the report and the page use."""
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


def prf(tp: int, fp: int, fn: int) -> dict:
    """Precision / recall / F1. A ratio with a zero denominator is None (undefined), never silently 0 or 1."""
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = (2 * p * r / (p + r)) if p is not None and r is not None and (p + r) > 0 else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


def brier(pairs: Sequence[tuple[float, bool]]) -> float | None:
    """Mean squared gap between stated confidence and what happened (0 = perfect). None for no data."""
    if not pairs:
        return None
    return sum((c - (1.0 if ok else 0.0)) ** 2 for c, ok in pairs) / len(pairs)


def reliability(pairs: Sequence[tuple[float, bool]], edges: Sequence[float]) -> list[dict]:
    """Group (confidence, correct) pairs into bins [e0,e1), [e1,e2) ... the last bin includes its upper edge."""
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        last = i == len(edges) - 2
        sel = [(c, ok) for c, ok in pairs if c >= lo and (c <= hi if last else c < hi)]
        n = len(sel)
        k = sum(1 for _, ok in sel if ok)
        row = {"lo": lo, "hi": hi, "n": n, "correct": k,
               "mean_confidence": (sum(c for c, _ in sel) / n) if n else None, "accuracy": (k / n) if n else None}
        row["ci95"] = [round(x, 4) for x in wilson(k, n)] if n else None
        out.append(row)
    return out


def risk_coverage(pairs: Sequence[tuple[float, bool]], thresholds: Iterable[float]) -> list[dict]:
    """If the system only kept fields with confidence >= t: what share is kept, and how often are they right?"""
    total = len(pairs)
    out = []
    for t in thresholds:
        kept = [(c, ok) for c, ok in pairs if c >= t]
        k = sum(1 for _, ok in kept if ok)
        out.append({"threshold": t, "kept": len(kept), "coverage": (len(kept) / total) if total else None,
                    "precision": (k / len(kept)) if kept else None,
                    "ci95": [round(x, 4) for x in wilson(k, len(kept))] if kept else None})
    return out


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, q in [0,100]. None for no data."""
    if not values:
        return None
    s = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(s)))
    return s[min(rank, len(s)) - 1]
