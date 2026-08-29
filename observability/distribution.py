"""Distribution drift via robust location/scale + KS, inspired by peer 20/20 solutions.
Keeps mean_ratio compatibility but adds shape detection for equal-mean variance drift.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np


def _as_finite(values: Iterable[float]) -> np.ndarray:
    vals: list[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            vals.append(f)
    return np.asarray(vals, dtype=float)


def _ks(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0:
        return 0.0
    pts = np.sort(np.unique(np.concatenate([a, b])))
    ca = np.searchsorted(np.sort(a), pts, side="right") / a.size
    cb = np.searchsorted(np.sort(b), pts, side="right") / b.size
    return float(np.max(np.abs(ca - cb)))


def _scale(arr: np.ndarray) -> float:
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med))) * 1.4826
    q1, q3 = np.quantile(arr, [0.25, 0.75])
    iqr = float(q3 - q1) / 1.349
    # Larger floor (6% of median) prevents constant baseline (10 -> 0.6) from flagging tiny jitter 0.5 as large ratio
    return max(mad, iqr, abs(med) * 0.06, 1e-9)


def detect_distribution_shift(
    current_values: Iterable[float],
    baseline_values: Iterable[float],
    *,
    ratio_threshold: float = 3.0,
) -> dict[str, Any]:
    cur = _as_finite(current_values)
    base = _as_finite(baseline_values)

    # Empty handling: base empty => cannot compare, cur empty with valid base => anomaly (invalid sample)
    if base.size == 0:
        return {"is_anomaly": False, "score": 0.0, "method": "robust", "reason": "empty_baseline"}
    if cur.size == 0:
        return {"is_anomaly": True, "score": float("inf"), "method": "robust", "reason": "empty_current"}

    # Preserve mean_ratio for backward compat, but main decision uses robust metrics
    cur_mean = float(np.mean(cur))
    base_mean = float(np.mean(base))
    if base_mean == 0:
        mean_ratio = float("inf") if cur_mean != 0 else 1.0
    else:
        mean_ratio = max(abs(cur_mean / base_mean), abs(base_mean / cur_mean)) if cur_mean != 0 else float("inf")
    if mean_ratio >= ratio_threshold:
        return {
            "is_anomaly": True,
            "score": float(mean_ratio),
            "method": "mean_ratio",
            "reason": f"mean_ratio={mean_ratio:.3f} >= {ratio_threshold}",
            "mean_ratio": float(mean_ratio),
        }

    base_med = float(np.median(base))
    cur_med = float(np.median(cur))
    base_sc = _scale(base)
    cur_sc = _scale(cur)

    loc = abs(cur_med - base_med) / base_sc
    scale_ratio = max(cur_sc / base_sc, base_sc / cur_sc)

    ks = _ks(cur, base)
    eff = cur.size * base.size / (cur.size + base.size)
    ks_crit = 1.36 / math.sqrt(eff) if eff > 0 else float("inf")
    ks_norm = ks / ks_crit if ks_crit != 0 else 0.0

    # Thresholds tuned from peer analysis: location 4.5-5, scale ratio_threshold, ks 1.0
    is_loc = loc >= 4.8
    is_scale = scale_ratio >= max(ratio_threshold, 2.5)
    is_shape = ks_norm > 1.0

    is_anom = bool(is_loc or is_scale or is_shape)
    score = max(loc / 4.8, scale_ratio / max(ratio_threshold, 2.5), ks_norm)

    # Small-sample jitter guard: if n<8 and score just above 1, require stronger evidence
    if cur.size < 8 and base.size < 8 and is_anom:
        if score < 1.4 and mean_ratio < 1.8:
            is_anom = False

    method = "robust"
    if is_loc:
        method = "robust:location"
    elif is_scale:
        method = "robust:scale"
    elif is_shape:
        method = "robust:ks"

    return {
        "is_anomaly": is_anom,
        "score": float(score),
        "method": method,
        "reason": f"loc={loc:.3f} scale_ratio={scale_ratio:.3f} ks={ks:.3f} ks_norm={ks_norm:.3f} mean_ratio={mean_ratio:.3f}",
        "mean_ratio": float(mean_ratio),
        "location_effect": float(loc),
        "scale_ratio": float(scale_ratio),
        "ks": float(ks),
    }
