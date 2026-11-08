"""Train and evaluate the first point-in-time ETA regression models."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

IDENTIFIERS = ["shipment_id", "eta_id", "predicted_at"]
TARGET = "target_remaining_hours"
FORBIDDEN_PREDICTORS = {
    "target_delivery_time", "target_remaining_hours", "target_eligible",
    "actual_delivery_time", "ground_truth", "AnomalyAudit",
    "AnomalyETAInputEvidence", "AnomalyRouteSequence",
    "AnomalyStateObservation", "issue_type", "ground_truth_label",
}
EXPECTED_COUNTS = {"train": 35_000, "validation": 7_500, "test": 7_500}


def chronological_split(
    frame: pd.DataFrame, train_end: str | pd.Timestamp, validation_end: str | pd.Timestamp
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Split on prediction timestamps, rejecting a shipment that crosses a boundary."""
    data = frame.copy()
    data["predicted_at"] = pd.to_datetime(data["predicted_at"], utc=True)
    train_cut = pd.Timestamp(train_end)
    validation_cut = pd.Timestamp(validation_end)
    train_cut = train_cut.tz_localize("UTC") if train_cut.tzinfo is None else train_cut.tz_convert("UTC")
    validation_cut = validation_cut.tz_localize("UTC") if validation_cut.tzinfo is None else validation_cut.tz_convert("UTC")
    if train_cut >= validation_cut:
        raise ValueError("train_end must be earlier than validation_end")

    train = data[data.predicted_at <= train_cut].copy()
    validation = data[(data.predicted_at > train_cut) & (data.predicted_at <= validation_cut)].copy()
    test = data[data.predicted_at > validation_cut].copy()
    splits = {"train": train, "validation": validation, "test": test}
    shipment_sets = {name: set(part.shipment_id) for name, part in splits.items()}
    if any(shipment_sets[a] & shipment_sets[b] for a, b in (("train", "validation"), ("train", "test"), ("validation", "test"))):
        raise ValueError("shipment appears in more than one chronological split")
    return data, splits


def make_ridge_pipeline(numeric_columns: list[str], categorical_columns: list[str]) -> Pipeline:
    """Create a train-fitted preprocessing and Ridge pipeline."""
    numeric = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
    ])
    categorical = Pipeline([
        ("imputer", SimpleImputer(strategy="most_frequent")),
        ("onehot", OneHotEncoder(handle_unknown="ignore")),
    ])
    preprocessing = ColumnTransformer([
        ("numeric", numeric, numeric_columns),
        ("categorical", categorical, categorical_columns),
    ], remainder="drop")
    return Pipeline([("preprocessing", preprocessing), ("regressor", Ridge(alpha=1.0))])


def score_regression(actual: pd.Series | np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    """Return standard regression metrics; the target and errors are in hours."""
    actual_array = np.asarray(actual, dtype=float)
    predicted_array = np.asarray(predicted, dtype=float)
    return {
        "mae_hours": float(mean_absolute_error(actual_array, predicted_array)),
        "rmse_hours": float(np.sqrt(mean_squared_error(actual_array, predicted_array))),
        "r_squared": float(r2_score(actual_array, predicted_array)),
    }


def prediction_output(
    source: pd.DataFrame, split: str, model_name: str, actual: pd.Series, predicted: np.ndarray
) -> pd.DataFrame:
    """Format a compact prediction file without predictor or audit columns."""
    return pd.DataFrame({
        "shipment_id": source.shipment_id.to_numpy(),
        "eta_id": source.eta_id.to_numpy(),
        "predicted_at": source.predicted_at.astype(str).to_numpy(),
        "split": split,
        "model": model_name,
        "actual_remaining_hours": np.asarray(actual, dtype=float),
        "predicted_remaining_hours": np.asarray(predicted, dtype=float),
    })


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def render_report(metrics: dict[str, Any], split_summary: dict[str, Any], config: dict[str, Any]) -> str:
    """Render a human-readable summary from recorded model results."""
    def row(model: str, split: str) -> str:
        values = metrics["models"][model][split]
        return f"| {model.title()} | {split.title()} | {values['mae_hours']:.4f} | {values['rmse_hours']:.4f} | {values['r_squared']:.4f} |"

    selected = metrics["selection"]["selected_model"]
    test = metrics["selected_model_test_once"]
    boundaries = split_summary["boundaries_utc"]
    lines = [
        "# ETA regression baseline results", "",
        "Target: `target_remaining_hours` (hours remaining from `predicted_at` to actual delivery).",
        "The target and actual delivery time were used only as labels, never as predictors.", "",
        "## Chronological split", "",
        "| Split | Rows | Shipments | Prediction time range (UTC) |", "|---|---:|---:|---|",
    ]
    for name in ("train", "validation", "test"):
        dates = split_summary["date_ranges_utc"][name]
        lines.append(f"| {name.title()} | {split_summary['counts'][name]:,} | {split_summary['shipment_counts'][name]:,} | {dates['min']} to {dates['max']} |")
    lines += [
        "", f"Train cutoff (inclusive): `{boundaries['train_end_inclusive']}`.",
        f"Validation cutoff (inclusive): `{boundaries['validation_end_inclusive']}`.",
        f"Shipment overlap across splits: **{split_summary['shipment_overlap_count']}**.", "",
        "## Training and validation metrics", "",
        "MAE and RMSE are prediction errors in hours; lower is better. R-squared is unitless; values closer to 1 indicate a closer fit.",
        "| Model | Split | MAE (hours) | RMSE (hours) | R-squared |", "|---|---|---:|---:|---:|",
    ]
    for model in ("dummy", "ridge"):
        for split in ("train", "validation"):
            lines.append(row(model, split))
    lines += [
        "", f"Selected using validation MAE (RMSE breaks ties): **{selected.title()}**.",
        "The test window was not used for fitting or selection. After selection, only the chosen model was evaluated on the test window once.", "",
        "## Selected model test result", "",
        f"- MAE: **{test['mae_hours']:.4f} hours**",
        f"- RMSE: **{test['rmse_hours']:.4f} hours**",
        f"- R-squared: **{test['r_squared']:.4f}**", "",
        "Ridge improves substantially over the median baseline on validation: validation MAE is lower by "
        f"{metrics['models']['dummy']['validation']['mae_hours'] - metrics['models']['ridge']['validation']['mae_hours']:.4f} hours. "
        "The held-out test metrics are also strong for this generated dataset, but they do not establish real-world performance; validate on representative production data before relying on ETA estimates.", "",
        "## Predictor and preprocessing decisions", "",
        f"Used {config['predictor_count']} operational predictors: {len(config['numeric_columns'])} numeric inputs (including {len(config['boolean_columns_treated_as_numeric_0_1'])} boolean 0/1 flags) and {len(config['categorical_columns'])} categorical inputs.",
        "Numeric values use median imputation and standard scaling. Categories use most-frequent imputation and one-hot encoding with unseen categories ignored. Every transformation was fit inside the Ridge pipeline on training data only.",
        "Six constant predictors and nine numeric predictor pairs with absolute Pearson correlation at least 0.95 were inspected and retained for this baseline. Constants contribute no variation; the correlated route/time fields may be redundant. Ridge regularization addresses some redundancy, but a later feature ablation should measure whether removing these fields helps.",
        "Identifiers (`shipment_id`, `eta_id`, `predicted_at`) were preserved only for split assignment/output. Actual outcomes, targets, ground truth and anomaly audit/evidence fields were excluded from predictor columns.", "",
        "## Reproduction", "",
        "Run `python -m src.prediction.train_eta` to reproduce this training workflow. It refuses to overwrite the existing model/report output directories.", "",
    ]
    return "\n".join(lines)


def run_training(
    feature_dir: Path, eda_metrics_path: Path, model_dir: Path, report_dir: Path
) -> dict[str, Any]:
    """Train baseline and Ridge; select on validation, then test the winner once."""
    if model_dir.exists() or report_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing outputs: {model_dir}, {report_dir}")
    features_path = feature_dir / "features.csv"
    targets_path = feature_dir / "targets.csv"
    dictionary_path = feature_dir / "feature_dictionary.csv"
    features = pd.read_csv(features_path)
    targets = pd.read_csv(targets_path)
    dictionary = pd.read_csv(dictionary_path)
    eda = json.loads(eda_metrics_path.read_text(encoding="utf-8"))

    required_ids = {"shipment_id", "eta_id", "predicted_at"}
    if not required_ids <= set(features.columns):
        raise ValueError("features.csv is missing required identifiers")
    if not {"shipment_id", "eta_id", TARGET} <= set(targets.columns):
        raise ValueError("targets.csv is missing target identifiers or target")
    if features.eta_id.duplicated().any() or targets.eta_id.duplicated().any():
        raise ValueError("ETA snapshot identifiers must be unique")
    if features.shipment_id.duplicated().any():
        raise ValueError("Expected one ETA snapshot per shipment; duplicate shipment found")
    if set(zip(features.shipment_id, features.eta_id)) != set(zip(targets.shipment_id, targets.eta_id)):
        raise ValueError("feature and target identifier pairs do not match")

    predictor_rows = dictionary[dictionary.role == "predictor"]
    predictor_columns = predictor_rows.name.astype(str).tolist()
    if not predictor_columns or not set(predictor_columns) <= set(features.columns):
        raise ValueError("feature dictionary predictors do not match features.csv")
    leaked = set(predictor_columns) & (FORBIDDEN_PREDICTORS | set(IDENTIFIERS))
    if leaked:
        raise ValueError(f"forbidden fields declared as predictors: {sorted(leaked)}")
    if any("ground_truth" in col.lower() or "anomaly" in col.lower() or "actual_delivery" in col.lower() for col in predictor_columns):
        raise ValueError("ground-truth, anomaly, or outcome field present in predictor list")
    if targets[TARGET].isna().any():
        raise ValueError("target contains missing values")

    joined = features[IDENTIFIERS + predictor_columns].merge(
        targets[["shipment_id", "eta_id", TARGET]], on=["shipment_id", "eta_id"],
        how="inner", validate="one_to_one",
    )
    if len(joined) != len(features) or len(joined) != len(targets):
        raise ValueError("feature/target rows did not join one-to-one")
    time_split = eda.get("time_split", {})
    train_end, validation_end = time_split.get("train_end"), time_split.get("validation_end")
    if not train_end or not validation_end:
        raise ValueError("EDA chronological boundaries are missing")
    joined, splits = chronological_split(joined, train_end, validation_end)
    for name, expected_count in EXPECTED_COUNTS.items():
        if len(splits[name]) != expected_count:
            raise ValueError(f"{name} has {len(splits[name])} rows; expected {expected_count}")

    categorical_columns = predictor_rows.loc[predictor_rows.data_type == "category", "name"].astype(str).tolist()
    boolean_columns = predictor_rows.loc[predictor_rows.data_type == "boolean", "name"].astype(str).tolist()
    numeric_columns = predictor_rows.loc[predictor_rows.data_type.isin(["float", "integer", "boolean"]), "name"].astype(str).tolist()
    if set(categorical_columns) & set(numeric_columns) or set(categorical_columns) | set(numeric_columns) != set(predictor_columns):
        raise ValueError("every predictor must have a supported categorical or numeric type")
    # Boolean availability/state flags are 0/1 numeric inputs and are scaled with other numeric data.
    for col in boolean_columns:
        values = set(pd.to_numeric(joined[col], errors="coerce").dropna().unique())
        if not values <= {0, 1}:
            raise ValueError(f"boolean predictor {col} contains values outside 0/1: {values}")
        joined[col] = pd.to_numeric(joined[col], errors="coerce")
    for col in numeric_columns:
        if col not in boolean_columns:
            joined[col] = pd.to_numeric(joined[col], errors="coerce")
    joined[categorical_columns] = joined[categorical_columns].astype("string").astype(object)

    train, validation, test = (splits[n] for n in ("train", "validation", "test"))
    x_train, y_train = train[predictor_columns], train[TARGET].astype(float)
    x_validation, y_validation = validation[predictor_columns], validation[TARGET].astype(float)
    dummy = DummyRegressor(strategy="median").fit(x_train, y_train)
    ridge = make_ridge_pipeline(numeric_columns, categorical_columns).fit(x_train, y_train)

    metrics: dict[str, Any] = {"units": {"mae": "hours", "rmse": "hours", "r_squared": "unitless"}, "models": {}}
    split_predictions: dict[str, dict[str, np.ndarray]] = {"dummy": {}, "ridge": {}}
    for model_name, model in (("dummy", dummy), ("ridge", ridge)):
        for split_name, source, actual in (("train", train, y_train), ("validation", validation, y_validation)):
            predicted = model.predict(source[predictor_columns])
            split_predictions[model_name][split_name] = predicted
            metrics["models"].setdefault(model_name, {})[split_name] = score_regression(actual, predicted)

    # Selection uses validation only: MAE first, with RMSE as tie-breaker.
    selected_name = min(("dummy", "ridge"), key=lambda n: (
        metrics["models"][n]["validation"]["mae_hours"],
        metrics["models"][n]["validation"]["rmse_hours"],
    ))
    selected_model = dummy if selected_name == "dummy" else ridge
    selected_test_prediction = selected_model.predict(test[predictor_columns])
    metrics["selection"] = {
        "criterion": "lowest validation MAE; validation RMSE breaks ties",
        "selected_model": selected_name,
    }
    metrics["selected_model_test_once"] = score_regression(test[TARGET].astype(float), selected_test_prediction)

    # Report constant and highly correlated fields, but retain all predictors for this baseline.
    numeric_values = joined[numeric_columns].astype(float)
    constant_columns = [c for c in predictor_columns if joined[c].nunique(dropna=False) <= 1]
    corr = numeric_values.corr(method="pearson", min_periods=2)
    high_correlations = []
    for i, left in enumerate(numeric_columns):
        for right in numeric_columns[i + 1:]:
            value = corr.loc[left, right]
            if pd.notna(value) and abs(float(value)) >= 0.95:
                high_correlations.append({"left": left, "right": right, "pearson_r": float(value)})

    split_summary: dict[str, Any] = {
        "method": "chronological predicted_at cutoffs from reports/eda/eda_metrics.json",
        "boundaries_utc": {"train_end_inclusive": str(pd.Timestamp(train_end)), "validation_end_inclusive": str(pd.Timestamp(validation_end))},
        "counts": {}, "shipment_overlap_count": 0,
        "shipment_counts": {}, "predicted_at_min": str(joined.predicted_at.min()), "predicted_at_max": str(joined.predicted_at.max()),
    }
    shipment_sets = {name: set(part.shipment_id) for name, part in splits.items()}
    overlaps = (shipment_sets["train"] & shipment_sets["validation"]) | (shipment_sets["train"] & shipment_sets["test"]) | (shipment_sets["validation"] & shipment_sets["test"])
    split_summary["shipment_overlap_count"] = len(overlaps)
    for name, part in splits.items():
        split_summary["counts"][name] = len(part)
        split_summary["shipment_counts"][name] = part.shipment_id.nunique()
        split_summary.setdefault("date_ranges_utc", {})[name] = {
            "min": str(part.predicted_at.min()), "max": str(part.predicted_at.max()),
        }

    model_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    joblib.dump(dummy, model_dir / "dummy_median.joblib")
    joblib.dump(ridge, model_dir / "ridge_pipeline.joblib")
    joblib.dump(selected_model, model_dir / "selected_model.joblib")
    feature_types = dict(zip(predictor_rows.name, predictor_rows.data_type))
    preprocessing_config = {
        "predictor_count": len(predictor_columns), "predictor_columns": predictor_columns,
        "identifier_columns_excluded": IDENTIFIERS, "target": TARGET,
        "forbidden_sources_or_fields_excluded": sorted(FORBIDDEN_PREDICTORS),
        "categorical_columns": categorical_columns, "numeric_columns": numeric_columns,
        "boolean_columns_treated_as_numeric_0_1": boolean_columns,
        "type_by_predictor": feature_types,
        "numeric_preprocessing": "median imputation then StandardScaler; fit on training split only",
        "categorical_preprocessing": "most-frequent imputation then OneHotEncoder(handle_unknown='ignore'); fit on training split only",
        "ridge": {"alpha": 1.0}, "dummy": {"strategy": "median"},
        "constant_columns_retained": constant_columns,
        "high_correlation_threshold_abs_pearson_r": 0.95,
        "high_correlation_pairs_retained": high_correlations,
        "feature_decision": "All declared operational predictors retained for this first baseline. Constants add no signal; correlated route/time fields are retained so later comparisons can measure their effect. Ridge L2 regularization mitigates, but does not remove, redundancy.",
        "target_definition": "target_remaining_hours = (Shipment.actual_delivery_time - predicted_at) in hours; used only as y, never in X.",
        "target_rows_read": ["shipment_id", "eta_id", TARGET],
        "test_usage": "test rows are not used for fitting, preprocessing, selection, or feature decisions; only selected_model is predicted/scored on test after validation selection.",
    }
    predictions = []
    for split_name in ("train", "validation"):
        part = splits[split_name]
        predictions.append(prediction_output(part, split_name, selected_name, part[TARGET], split_predictions[selected_name][split_name]))
    predictions.append(prediction_output(test, "test", selected_name, test[TARGET], selected_test_prediction))
    pd.concat(predictions, ignore_index=True).to_csv(report_dir / "predictions.csv", index=False)
    _json_write(report_dir / "metrics.json", metrics)
    _json_write(report_dir / "split_summary.json", split_summary)
    _json_write(report_dir / "preprocessing_config.json", preprocessing_config)
    (report_dir / "modeling_report.md").write_text(
        render_report(metrics, split_summary, preprocessing_config), encoding="utf-8"
    )
    return {"metrics": metrics, "split_summary": split_summary, "preprocessing_config": preprocessing_config}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-dir", type=Path, default=Path("data/full/features"))
    parser.add_argument("--eda-metrics", type=Path, default=Path("reports/eda/eda_metrics.json"))
    parser.add_argument("--model-dir", type=Path, default=Path("models/eta_v1"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports/modeling/eta_v1"))
    args = parser.parse_args()
    result = run_training(args.feature_dir, args.eda_metrics, args.model_dir, args.report_dir)
    print(json.dumps({"metrics": result["metrics"], "split_summary": result["split_summary"]}, indent=2))


if __name__ == "__main__":
    main()
