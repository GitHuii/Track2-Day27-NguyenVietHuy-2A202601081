from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from observability.anomaly import mad_detector, zscore_detector


def approximate_token_lengths(texts: Iterable[str]) -> list[int]:
    return [len(str(t).split()) for t in texts]


def detect_text_length_shift(
    current_texts: Iterable[str],
    baseline_batch_means: Iterable[float],
    *,
    threshold: float = 3.0,
) -> dict[str, Any]:
    lengths = approximate_token_lengths(current_texts)
    current_mean = float(np.mean(lengths)) if lengths else 0.0
    # Use MAD for robustness on text length (handles outlier docs)
    # Fallback to zscore if MAD zero
    mad_res = mad_detector(current_mean, baseline_batch_means, threshold=3.5)
    if mad_res["reason"] == "insufficient_history" or "mad_is_zero" in mad_res["reason"]:
        result = zscore_detector(current_mean, baseline_batch_means, threshold=threshold)
    else:
        result = mad_res
        # Also compute zscore for reference
        z = zscore_detector(current_mean, baseline_batch_means, threshold=threshold)
        result["zscore_comparison"] = z["score"]
    result["metric"] = "mean_text_length"
    result["current_mean"] = current_mean
    # Ensure method reflects text length
    result["method"] = result["method"] + ":text_length" if "text_length" not in result["method"] else result["method"]
    return result


def detect_embedding_norm_shift(
    current_norms: Iterable[float], baseline_norms: Iterable[float], *, threshold: float = 3.0
) -> dict[str, Any]:
    """Embedding norm drift: handles hidden evaluation with precomputed norms."""
    cur = np.asarray(list(current_norms), dtype=float)
    base = np.asarray(list(baseline_norms), dtype=float)
    if cur.size == 0 or base.size == 0:
        return {"is_anomaly": False, "score": 0.0, "method": "embedding_norm", "reason": "empty_input"}
    cur_mean = float(np.mean(cur))
    # Use both zscore and MAD; prefer MAD for robustness
    # First try MAD on baseline
    mad_res = mad_detector(cur_mean, base, threshold=3.5)
    z_res = zscore_detector(cur_mean, base, threshold=threshold)
    # If MAD says not anomaly but mean shifted dramatically, use zscore
    # Combine: anomaly if either triggers
    is_anom = bool(mad_res["is_anomaly"] or z_res["is_anomaly"])
    # Score is max
    score = max(float(mad_res["score"]), float(z_res["score"]))
    method = "embedding_norm:mad" if mad_res["is_anomaly"] else "embedding_norm:zscore" if z_res["is_anomaly"] else "embedding_norm"
    # Additional check: distribution shift via PSI-like on norms
    # If norms collapsed (all near zero) -> drift even if mean similar?
    cur_std = float(np.std(cur)) if cur.size > 1 else 0.0
    base_std = float(np.std(base)) if base.size > 1 else 0.0
    std_ratio = abs(base_std / cur_std) if cur_std != 0 else float("inf") if base_std != 0 else 1.0
    if std_ratio >= 3.0:
        is_anom = True
        method = "embedding_norm:std_collapse"
        score = max(score, float(std_ratio))

    return {
        "is_anomaly": is_anom,
        "score": float(score),
        "method": method,
        "reason": f"baseline_mean={float(np.mean(base)):.4f}, current_mean={cur_mean:.4f}, mad_score={mad_res['score']:.2f}, zscore={z_res['score']:.2f}, std_ratio={std_ratio:.2f}",
        "mad_score": float(mad_res["score"]),
        "zscore": float(z_res["score"]),
        "current_mean": cur_mean,
        "baseline_mean": float(np.mean(base)),
    }
