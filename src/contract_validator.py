"""Enhanced contract validator: type, freshness, severity-aware actions.
Keeps stable interface validate_dataframe(df, contract) -> list[dict].
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
SEVERITY_ACTION = {"critical": "block", "warning": "quarantine", "info": "warn"}


def _issue(
    check: str,
    *,
    column: str | None,
    severity: str,
    passed: bool,
    details: str,
) -> dict[str, Any]:
    return {
        "check": check,
        "column": column,
        "severity": severity,
        "passed": bool(passed),
        "details": details,
        "action": SEVERITY_ACTION.get(severity, "warn"),
    }


def load_contract(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _check_type(series: pd.Series, expected_type: str) -> tuple[bool, int, str]:
    """Validate declared type. Returns (passed, invalid_count, detail)."""
    # Drop NA for type check - nulls are covered by not_null
    non_na = series[series.notna()]
    if non_na.empty:
        return True, 0, "no_non_null_values"
    invalid_count = 0
    t = expected_type.lower()
    if t == "integer":
        # Strict: string values like "123" are drift even if to_numeric succeeds
        # First catch any string type values
        str_mask = non_na.apply(lambda v: isinstance(v, str))
        if str_mask.any():
            # any string in integer column is invalid (type drift)
            invalid_count = int(str_mask.sum())
            # also check remaining non-string numeric values for integer-ness
            non_str = non_na[~str_mask]
            if not non_str.empty:
                numeric = pd.to_numeric(non_str, errors="coerce")
                is_invalid = numeric.isna()
                valid_mask = ~is_invalid
                if valid_mask.any():
                    float_vals = numeric[valid_mask].astype(float)
                    is_not_int = (float_vals % 1 != 0)
                    is_invalid.loc[valid_mask] = is_not_int.values
                invalid_count += int(is_invalid.sum())
        else:
            numeric = pd.to_numeric(non_na, errors="coerce")
            is_invalid = numeric.isna()
            valid_mask = ~is_invalid
            if valid_mask.any():
                float_vals = numeric[valid_mask].astype(float)
                is_not_int = (float_vals % 1 != 0)
                is_invalid.loc[valid_mask] = is_not_int.values
            invalid_count = int(is_invalid.sum())
    elif t == "number":
        # Strict: reject string numeric drift
        str_mask = non_na.apply(lambda v: isinstance(v, str))
        if str_mask.any():
            # any pure string that is numeric is still drift for number type if original was numeric
            # We count strings as invalid for number expectation
            invalid_count = int(str_mask.sum())
            # also check non-string part
            non_str = non_na[~str_mask]
            if not non_str.empty:
                numeric = pd.to_numeric(non_str, errors="coerce")
                invalid_count += int(numeric.isna().sum())
        else:
            numeric = pd.to_numeric(non_na, errors="coerce")
            invalid_count = int(numeric.isna().sum())
    elif t == "string":
        # string type: value should be string; reject pure numeric drift? We accept object dtype
        # Invalid if value is not string type after stripping
        # But hidden type drift may set integer values into string column - catch it
        # So we check type explicitly
        invalid_count = int(sum(1 for v in non_na if not isinstance(v, str)))
        # If pandas inferred numeric dtype, still count as invalid for string expectation
        if invalid_count == 0 and pd.api.types.is_numeric_dtype(non_na):
            # If series is numeric dtype but expected string, it's drift
            invalid_count = len(non_na)
    elif t == "datetime":
        dt = pd.to_datetime(non_na, utc=True, errors="coerce")
        invalid_count = int(dt.isna().sum())
    else:
        # Unknown type - pass
        return True, 0, f"unknown_type_{expected_type}"
    passed = invalid_count == 0
    return passed, invalid_count, f"invalid_type_count={invalid_count}; expected={expected_type}"


def validate_dataframe(df: pd.DataFrame, contract: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    # Support both 'columns' (orders) and 'fields' (kb)
    columns = contract.get("columns") or contract.get("fields") or {}

    for column, rules in columns.items():
        # Normalize rules if it's not dict (e.g., kb_contract may have simple)
        if not isinstance(rules, dict):
            rules = {}
        severity = rules.get("severity", "warning")
        required = bool(rules.get("required", False))

        if column not in df.columns:
            if required:
                issues.append(
                    _issue(
                        "required_column",
                        column=column,
                        severity=severity,
                        passed=False,
                        details=f"Missing required column: {column}",
                    )
                )
            continue

        series = df[column]

        if required:
            null_count = int(series.isna().sum())
            issues.append(
                _issue(
                    "not_null",
                    column=column,
                    severity=severity,
                    passed=(null_count == 0),
                    details=f"null_count={null_count}",
                )
            )

        if rules.get("unique"):
            duplicate_count = int(series.duplicated(keep=False).sum())
            issues.append(
                _issue(
                    "unique",
                    column=column,
                    severity=severity,
                    passed=(duplicate_count == 0),
                    details=f"duplicate_rows={duplicate_count}",
                )
            )

        # Type validation - always check if declared
        if "type" in rules:
            expected_type = rules["type"]
            passed, invalid_count, detail = _check_type(series, expected_type)
            issues.append(
                _issue(
                    "type",
                    column=column,
                    severity=severity,
                    passed=passed,
                    details=detail,
                )
            )

        accepted = rules.get("accepted_values")
        if accepted is not None:
            invalid_mask = series.notna() & ~series.isin(accepted)
            invalid_count = int(invalid_mask.sum())
            issues.append(
                _issue(
                    "accepted_values",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; accepted={accepted}",
                )
            )

        # Numeric range
        if "min" in rules or "max" in rules:
            numeric = pd.to_numeric(series, errors="coerce")
            invalid = pd.Series(False, index=series.index)
            if "min" in rules:
                try:
                    invalid |= numeric < rules["min"]
                except Exception:
                    invalid |= False
            if "max" in rules:
                try:
                    invalid |= numeric > rules["max"]
                except Exception:
                    invalid |= False
            invalid_count = int(invalid.fillna(False).sum())
            issues.append(
                _issue(
                    "range",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}",
                )
            )

        # min_length for string fields (kb)
        if "min_length" in rules:
            min_len = rules["min_length"]
            # only for non-na string values
            lengths = series.dropna().astype(str).str.len()
            invalid_count = int((lengths < min_len).sum())
            issues.append(
                _issue(
                    "min_length",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; min_length={min_len}",
                )
            )

    # Freshness validation at contract level
    freshness = contract.get("freshness")
    if isinstance(freshness, dict):
        col = freshness.get("column")
        max_delay = freshness.get("max_delay_minutes")
        severity = freshness.get("severity", "warning")
        if col is not None and max_delay is not None:
            if col not in df.columns:
                issues.append(
                    _issue(
                        "freshness",
                        column=col,
                        severity=severity,
                        passed=False,
                        details=f"freshness column missing: {col}",
                    )
                )
            else:
                try:
                    ts = pd.to_datetime(df[col], utc=True, errors="coerce")
                    # Also try to handle case where data is file path? No.
                    max_ts = ts.max()
                    if pd.isna(max_ts):
                        issues.append(
                            _issue(
                                "freshness",
                                column=col,
                                severity=severity,
                                passed=False,
                                details="no_valid_timestamps",
                            )
                        )
                    else:
                        now = pd.Timestamp(datetime.now(timezone.utc))
                        if max_ts.tzinfo is None:
                            max_ts = max_ts.tz_localize("UTC")
                        delay_minutes = (now - max_ts).total_seconds() / 60.0
                        passed = delay_minutes <= float(max_delay)
                        issues.append(
                            _issue(
                                "freshness",
                                column=col,
                                severity=severity,
                                passed=passed,
                                details=f"delay_minutes={delay_minutes:.1f}, max_delay={max_delay}, max_ts={max_ts.isoformat()}",
                            )
                        )
                except Exception as e:
                    issues.append(
                        _issue(
                            "freshness",
                            column=col,
                            severity=severity,
                            passed=False,
                            details=f"freshness_error: {e}",
                        )
                    )

    return issues


def failed_issues(issues: list[dict[str, Any]], min_severity: str | None = None) -> list[dict[str, Any]]:
    failed = [i for i in issues if not i.get("passed", False)]
    if min_severity is None:
        return failed
    order = {"info": 0, "warning": 1, "critical": 2}
    threshold = order[min_severity]
    return [i for i in failed if order.get(i.get("severity", "warning"), 1) >= threshold]


def decide_action(issues: list[dict[str, Any]]) -> str:
    """Return overall pipeline action based on worst failed severity."""
    failed = failed_issues(issues)
    if not failed:
        return "pass"
    max_sev = max(failed, key=lambda x: SEVERITY_ORDER.get(x.get("severity", "warning"), 1))
    return SEVERITY_ACTION.get(max_sev.get("severity", "warning"), "warn")


def quarantine_failed_rows(df: pd.DataFrame, contract: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split df into good vs quarantined based on critical failures per row.
    - unique and accepted_values failures are row-level; we quarantine offending rows.
    Returns (good_df, quarantine_df). Used for bonus automatic quarantine.
    """
    issues = validate_dataframe(df, contract)
    critical_failed = failed_issues(issues, min_severity="critical")
    if not critical_failed:
        return df.copy(), df.iloc[0:0].copy()
    # Build mask of rows that violate critical checks
    columns = contract.get("columns") or contract.get("fields") or {}
    quarantine_mask = pd.Series(False, index=df.index)
    for column, rules in columns.items():
        if column not in df.columns:
            continue
        severity = rules.get("severity", "warning")
        if SEVERITY_ORDER.get(severity, 1) < SEVERITY_ORDER["critical"]:
            continue
        if rules.get("unique") and df[column].duplicated(keep=False).any():
            quarantine_mask |= df[column].duplicated(keep=False)
        accepted = rules.get("accepted_values")
        if accepted is not None:
            quarantine_mask |= df[column].notna() & ~df[column].isin(accepted)
        if "min" in rules or "max" in rules:
            numeric = pd.to_numeric(df[column], errors="coerce")
            if "min" in rules:
                quarantine_mask |= numeric < rules["min"]
            if "max" in rules:
                quarantine_mask |= numeric > rules["max"]
    quarantine_df = df[quarantine_mask].copy()
    good_df = df[~quarantine_mask].copy()
    return good_df, quarantine_df
