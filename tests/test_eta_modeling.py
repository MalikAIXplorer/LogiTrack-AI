import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.prediction.train_eta import (
    chronological_split,
    make_ridge_pipeline,
    prediction_output,
)


PROJECT = Path(__file__).resolve().parents[1]


def test_full_data_chronological_split_has_exact_sizes_and_no_shipment_overlap():
    features = pd.read_csv(PROJECT / "data/full/features/features.csv", usecols=["shipment_id", "eta_id", "predicted_at"])
    boundaries = json.loads((PROJECT / "reports/eda/eda_metrics.json").read_text(encoding="utf-8"))["time_split"]
    _, splits = chronological_split(features, boundaries["train_end"], boundaries["validation_end"])
    assert {key: len(value) for key, value in splits.items()} == {
        "train": 35_000, "validation": 7_500, "test": 7_500,
    }
    shipment_sets = {key: set(value.shipment_id) for key, value in splits.items()}
    assert not (shipment_sets["train"] & shipment_sets["validation"])
    assert not (shipment_sets["train"] & shipment_sets["test"])
    assert not (shipment_sets["validation"] & shipment_sets["test"])
    assert splits["train"].predicted_at.max() <= splits["validation"].predicted_at.min()
    assert splits["validation"].predicted_at.max() <= splits["test"].predicted_at.min()


def test_chronological_split_rejects_shipment_across_periods():
    data = pd.DataFrame({
        "shipment_id": ["S1", "S1"],
        "eta_id": ["E1", "E2"],
        "predicted_at": ["2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"],
    })
    with pytest.raises(ValueError, match="shipment appears"):
        chronological_split(data, "2026-01-01T12:00:00Z", "2026-01-02T12:00:00Z")


def test_preprocessing_fits_with_missing_values_categories_and_boolean_flags():
    x_train = pd.DataFrame({
        "number": [1.0, np.nan, 3.0, 4.0],
        "flag": [0, 1, 0, 1],
        "category": ["a", "b", None, "a"],
    })
    x_validation = pd.DataFrame({"number": [np.nan], "flag": [1], "category": ["unseen"]})
    pipeline = make_ridge_pipeline(["number", "flag"], ["category"])
    pipeline.fit(x_train, [1.0, 2.0, 3.0, 4.0])
    prediction = pipeline.predict(x_validation)
    assert prediction.shape == (1,)
    assert np.isfinite(prediction).all()
    fitted = pipeline.named_steps["preprocessing"]
    assert fitted.named_transformers_["numeric"].named_steps["imputer"].statistics_[0] == 3.0


def test_prediction_output_contains_ids_and_target_but_no_predictors_or_truth_fields():
    source = pd.DataFrame({
        "shipment_id": ["SHP-2026-000001"],
        "eta_id": ["ETA-SHP-2026-000001-20260101000000"],
        "predicted_at": pd.to_datetime(["2026-01-01T00:00:00Z"], utc=True),
        "actual_delivery_time": ["must not be copied"],
        "ground_truth_label": ["must not be copied"],
    })
    output = prediction_output(source, "validation", "ridge", pd.Series([5.0]), np.array([4.5]))
    assert output.columns.tolist() == [
        "shipment_id", "eta_id", "predicted_at", "split", "model",
        "actual_remaining_hours", "predicted_remaining_hours",
    ]
    assert output.iloc[0].predicted_remaining_hours == 4.5
    assert not {"actual_delivery_time", "ground_truth_label"} & set(output.columns)
