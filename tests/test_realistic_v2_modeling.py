import numpy as np
import pandas as pd
import pytest

from src.prediction.train_realistic_v2 import (
    chronological_split,
    make_pipeline,
    prediction_frame,
    validate_feature_schema,
)


def test_chronological_split_is_reproducible_and_shipment_isolated():
    frame = pd.DataFrame({
        "shipment_id": [f"S{i:02}" for i in range(20)],
        "eta_id": [f"E{i:02}" for i in range(20)],
        "predicted_at": [f"2026-01-{1 + i // 2:02}T00:00:00Z" for i in range(20)],
    })
    first, bounds1 = chronological_split(frame, .6, .2)
    second, bounds2 = chronological_split(frame, .6, .2)
    assert bounds1 == bounds2
    assert {k: v.eta_id.tolist() for k, v in first.items()} == {
        k: v.eta_id.tolist() for k, v in second.items()
    }
    shipment_sets = {k: set(v.shipment_id) for k, v in first.items()}
    assert not shipment_sets["train"] & shipment_sets["validation"]
    assert not shipment_sets["train"] & shipment_sets["test"]
    assert not shipment_sets["validation"] & shipment_sets["test"]
    assert first["train"].predicted_at.max() < first["validation"].predicted_at.min()
    assert first["validation"].predicted_at.max() < first["test"].predicted_at.min()


def test_predictor_schema_rejects_actual_target_and_audit_fields():
    checks = {
        "event_cutoff_applied_twice": True,
        "target_fields_absent_from_predictors": True,
        "identifiers_not_predictors": True,
        "audit_truth_tables_not_feature_sources": True,
        "carrier_history_excludes_current_and_requires_both_delivery_times": True,
        "queue_requires_observed_and_ingestion_cutoffs": True,
        "realized_weather_requires_observed_and_ingestion_cutoffs": True,
        "forecast_requires_issue_and_ingestion_cutoffs": True,
        "route_plan_requires_known_at_cutoff": True,
        "outcome_and_route_change_tables_excluded": True,
    }
    summary = {
        "validation_status": "PASS",
        "checks": checks,
        "event_cutoff_rule": "event_time <= predicted_at AND ingestion_time <= predicted_at",
        "predictor_features": ["safe_feature", "actual_delivery_time"],
    }
    dictionary = pd.DataFrame([
        {"name": "safe_feature", "role": "predictor", "data_type": "float"},
        {"name": "actual_delivery_time", "role": "predictor", "data_type": "float"},
    ])
    with pytest.raises(ValueError, match="forbidden predictor"):
        validate_feature_schema(
            ["shipment_id", "eta_id", "predicted_at", "safe_feature", "actual_delivery_time"],
            ["shipment_id", "eta_id", "target_remaining_hours", "target_eligible"],
            dictionary, summary,
        )


def test_preprocessing_and_prediction_output_are_reproducible_and_clean():
    x_train = pd.DataFrame({
        "amount": [1.0, np.nan, 3.0, 4.0, 2.0, 8.0],
        "is_known": [0, 1, 0, 1, 0, 1],
        "kind": ["a", "b", None, "a", "c", "b"],
    })
    y_train = np.array([2.0, 4.0, 3.0, 7.0, 2.5, 8.0])
    x_predict = pd.DataFrame({"amount": [np.nan, 5.0], "is_known": [1, 0], "kind": ["new", "a"]})
    first = make_pipeline(["amount", "is_known"], ["kind"]).fit(x_train, y_train)
    second = make_pipeline(["amount", "is_known"], ["kind"]).fit(x_train, y_train)
    first_pred, second_pred = first.predict(x_predict), second.predict(x_predict)
    np.testing.assert_array_equal(first_pred, second_pred)
    source = pd.DataFrame({
        "shipment_id": ["S1", "S2"], "eta_id": ["E1", "E2"],
        "predicted_at": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], utc=True),
        "target_remaining_hours": [3.0, 4.0],
        "actual_delivery_time": ["private", "private"],
        "ground_truth": [1, 0],
    })
    output = prediction_frame(source, "test", "ridge", first_pred)
    assert output.columns.tolist() == [
        "shipment_id", "eta_id", "predicted_at", "split", "model",
        "actual_remaining_hours", "predicted_remaining_hours",
    ]
    assert output.eta_id.tolist() == ["E1", "E2"]
    assert np.isfinite(output.predicted_remaining_hours).all()
    assert not {"actual_delivery_time", "ground_truth"} & set(output.columns)
