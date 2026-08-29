#!/usr/bin/env python3
"""GX 1.21 Suite / ValidationDefinition / Checkpoint with severity-aware actions."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import great_expectations as gx
    from great_expectations.core.expectation_suite import ExpectationSuite
except ImportError as exc:
    raise SystemExit("great_expectations is not installed. Run: pip install -r requirements.txt") from exc


def build_orders_suite() -> gx.ExpectationSuite:
    """Build reusable ExpectationSuite with severity metadata."""
    suite = gx.ExpectationSuite(name="orders_suite")
    # Critical: uniqueness, not null, range
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(column="order_id", severity="critical", meta={"severity": "critical", "action": "block"})
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeUnique(column="order_id", severity="critical", meta={"severity": "critical", "action": "block"})
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(column="customer_id", severity="critical", meta={"severity": "critical", "action": "block"})
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeBetween(column="amount", min_value=0, severity="critical", meta={"severity": "critical", "action": "block"})
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeInSet(column="currency", value_set=["USD", "VND"], severity="critical", meta={"severity": "critical", "action": "block"})
    )
    # Warning: status accepted values, freshness handled separately
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToBeInSet(
            column="status", value_set=["pending", "completed", "refunded", "cancelled"], severity="warning", meta={"severity": "warning", "action": "quarantine"}
        )
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(column="created_at", severity="critical", meta={"severity": "critical", "action": "block"})
    )
    suite.add_expectation(
        gx.expectations.ExpectColumnValuesToNotBeNull(column="updated_at", severity="critical", meta={"severity": "critical", "action": "block"})
    )
    # Type expectations via regex / between? For demo, add expect column values to match string for customer_id
    # Note: GX 1.21 ExpectColumnValuesToMatchRegex for string pattern
    return suite


def get_validation_definition(context: Any, batch_definition: Any, suite: gx.ExpectationSuite):
    """Create or reuse ValidationDefinition."""
    try:
        # GX 1.x API
        validation_def = gx.ValidationDefinition(data=batch_definition, suite=suite, name="orders_validation")
        context.validation_definitions.add(validation_def)
        return validation_def
    except Exception as e:
        # Fallback if already exists
        try:
            return context.validation_definitions.get("orders_validation")
        except Exception:
            raise e


def evaluate_with_actions(results: list[Any]) -> dict[str, Any]:
    """Severity-aware action evaluation (reproduces block/quarantine/warn)."""
    critical_fail = any(not r.success and (getattr(r.expectation_config, "meta", {}) or {}).get("severity") == "critical" or getattr(r.expectation_config, "kwargs", {}).get("severity") == "critical" or "critical" in str(r.expectation_config) for r in results)
    # Simpler: check expectation severity attribute
    has_critical = False
    has_warning = False
    for r in results:
        exp = r.expectation_config
        # GX stores severity in kwargs or meta
        sev = None
        try:
            sev = exp.kwargs.get("severity") or exp.meta.get("severity")  # type: ignore
        except Exception:
            sev = None
        if not r.success:
            if sev == "critical":
                has_critical = True
            elif sev == "warning":
                has_warning = True
            else:
                # default warning
                has_warning = True
    if has_critical:
        action = "block"
    elif has_warning:
        action = "quarantine"
    else:
        action = "warn" if any(not r.success for r in results) else "pass"
    return {"critical_fail": has_critical, "warning_fail": has_warning, "action": action}


def main() -> None:
    df = pd.read_csv(ROOT / "data" / "incoming" / "orders.csv")
    context = gx.get_context(mode="ephemeral")

    # Data source / asset / batch definition
    data_source = context.data_sources.add_pandas("orders_pandas")
    asset = data_source.add_dataframe_asset(name="orders_dataframe")
    batch_definition = asset.add_batch_definition_whole_dataframe("whole_orders")

    # Suite / ValidationDefinition
    suite = build_orders_suite()
    try:
        context.suites.add(suite)
    except Exception:
        # suite may already exist in ephemeral context reuse
        pass

    validation_def = get_validation_definition(context, batch_definition, suite)

    # Checkpoint with result_format and actions
    # In GX 1.21, Checkpoint can be created via context.checkpoints.add
    try:
        checkpoint = gx.Checkpoint(
            name="orders_checkpoint",
            validation_definitions=[validation_def],
            actions=[],
            result_format={"result_format": "SUMMARY"},
        )
        context.checkpoints.add(checkpoint)
    except Exception as e:
        # Fallback to simple validation without checkpoint persistence
        print(f"Checkpoint creation fallback: {e}")
        checkpoint = None

    # Run validation
    if checkpoint is not None:
        try:
            checkpoint_result = checkpoint.run(batch_parameters={"dataframe": df})
            # Extract expectation results
            # In ephemeral checkpoint_result, use .run_results
            print("=== GX Checkpoint Result ===")
            print(checkpoint_result)
            # Determine overall success
            # For demo, also run batch.validate loop for detailed per-expectation logs
        except Exception as e:
            print(f"Checkpoint run failed, falling back to batch.validate: {e}")
            checkpoint = None

    if checkpoint is None:
        batch = batch_definition.get_batch(batch_parameters={"dataframe": df})
        all_ok = True
        results = []
        for exp in suite.expectations:
            r = batch.validate(exp)
            results.append(r)
            all_ok = all_ok and bool(r.success)
            sev = "unknown"
            try:
                sev = exp.kwargs.get("severity") or exp.meta.get("severity", "unknown")  # type: ignore
            except Exception:
                pass
            print(f"{exp.__class__.__name__:<45} severity={sev:<8} success={r.success}")

        action_eval = evaluate_with_actions(results)
        print(f"\nGX Suite result: {'PASS' if all_ok else 'FAIL'}  action={action_eval['action']}")
        print(f"Critical fail: {action_eval['critical_fail']}, Warning fail: {action_eval['warning_fail']}")
        # Demonstrate quarantine: if critical, would block pipeline
        if action_eval["action"] == "block":
            print("ACTION: BLOCK pipeline - critical contract failure")
        elif action_eval["action"] == "quarantine":
            print("ACTION: QUARANTINE - warning failures, isolate bad rows")
        else:
            print("ACTION: WARN/PASS - log and continue")
        return

    # If checkpoint succeeded, also show per-expectation via batch.validate for transparency
    batch = batch_definition.get_batch(batch_parameters={"dataframe": df})
    for exp in suite.expectations:
        r = batch.validate(exp)
        sev = "unknown"
        try:
            sev = exp.kwargs.get("severity") or exp.meta.get("severity", "unknown")  # type: ignore
        except Exception:
            pass
        print(f"{exp.__class__.__name__:<45} severity={sev:<8} success={r.success}")

    print("\nCheckpoint + Suite + ValidationDefinition completed with severity-aware actions.")


if __name__ == "__main__":
    main()
