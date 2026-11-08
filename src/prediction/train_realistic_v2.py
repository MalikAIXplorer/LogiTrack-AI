"""Train isolated ETA baselines from the realistic_v2 feature bundle."""
from __future__ import annotations
import argparse
import hashlib
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
FORBIDDEN = {
    "actual_delivery_time", "actual_pickup_time", "target_delivery_time",
    "target_remaining_hours", "target_eligible", "on_time", "delayed",
    "delay_minutes", "delay_reason_code", "ground_truth", "anomaly_type",
    "anomaly_id", "anomaly_evidence_id", "issue_type", "expected_value",
    "observed_value",
}


def validate_feature_schema(feature_columns: list[str], target_columns: list[str],
                            dictionary: pd.DataFrame,
                            validation_summary: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Return predictor/type groups after checking point-in-time metadata and leakage."""
    if validation_summary.get("validation_status") != "PASS":
        raise ValueError("realistic_v2 feature validation summary is not PASS")
    checks = validation_summary.get("checks", {})
    required = (
        "event_cutoff_applied_twice", "target_fields_absent_from_predictors",
        "identifiers_not_predictors", "audit_truth_tables_not_feature_sources",
        "carrier_history_excludes_current_and_requires_both_delivery_times",
        "queue_requires_observed_and_ingestion_cutoffs",
        "realized_weather_requires_observed_and_ingestion_cutoffs",
        "forecast_requires_issue_and_ingestion_cutoffs",
        "route_plan_requires_known_at_cutoff", "outcome_and_route_change_tables_excluded",
    )
    if any(checks.get(name) is not True for name in required):
        raise ValueError("point-in-time or leakage validation checks failed or are missing")
    if validation_summary.get("event_cutoff_rule") != "event_time <= predicted_at AND ingestion_time <= predicted_at":
        raise ValueError("feature bundle does not declare both event availability cutoffs")
    if not set(IDENTIFIERS) <= set(feature_columns):
        raise ValueError("features.csv is missing snapshot identifiers")
    if not {"shipment_id", "eta_id", TARGET, "target_eligible"} <= set(target_columns):
        raise ValueError("targets.csv is missing required identifiers or target fields")
    if not {"name", "role", "data_type"} <= set(dictionary.columns):
        raise ValueError("feature dictionary is missing required metadata")
    if dictionary.name.duplicated().any():
        raise ValueError("feature dictionary contains duplicate field names")
    rows = dictionary[dictionary.role == "predictor"]
    predictors = rows.name.astype(str).tolist()
    if not predictors or not set(predictors) <= set(feature_columns):
        raise ValueError("predictor dictionary is empty or names absent feature columns")
    forbidden = set(predictors) & (set(IDENTIFIERS) | FORBIDDEN)
    forbidden |= {n for n in predictors if any(x in n.lower() for x in ("ground_truth", "anomaly", "actual_delivery"))}
    if forbidden:
        raise ValueError(f"forbidden predictor fields: {sorted(forbidden)}")
    if set(predictors) != set(validation_summary.get("predictor_features", [])):
        raise ValueError("feature dictionary and validation summary predictor lists differ")
    type_map = dict(zip(rows.name.astype(str), rows.data_type.astype(str)))
    allowed = {"float", "integer", "boolean", "category"}
    if set(type_map.values()) - allowed:
        raise ValueError("unsupported predictor types in feature dictionary")
    numeric = [n for n in predictors if type_map[n] in {"float", "integer", "boolean"}]
    categorical = [n for n in predictors if type_map[n] == "category"]
    return predictors, numeric, categorical


def chronological_split(frame: pd.DataFrame, train_fraction: float = .70,
                        validation_fraction: float = .15) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Make deterministic timestamp cutoffs; all tied timestamps stay together."""
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 or train_fraction + validation_fraction >= 1:
        raise ValueError("split fractions must be positive and sum to less than one")
    data = frame.copy()
    data["predicted_at"] = pd.to_datetime(data.predicted_at, utc=True, errors="raise")
    if data.shipment_id.duplicated().any():
        raise ValueError("expected one ETA snapshot per shipment for time splitting")
    ordered = data.sort_values(["predicted_at", "shipment_id", "eta_id"], kind="mergesort")
    n = len(ordered)
    if n < 3:
        raise ValueError("at least three ETA snapshots are required")
    train_end = ordered.iloc[min(n - 2, max(0, int(n * train_fraction) - 1))].predicted_at
    val_end = ordered.iloc[min(n - 1, max(1, int(n * (train_fraction + validation_fraction)) - 1))].predicted_at
    splits = {
        "train": ordered[ordered.predicted_at <= train_end].copy(),
        "validation": ordered[(ordered.predicted_at > train_end) & (ordered.predicted_at <= val_end)].copy(),
        "test": ordered[ordered.predicted_at > val_end].copy(),
    }
    if any(part.empty for part in splits.values()):
        raise ValueError("chronological split produced an empty partition")
    sets = {name: set(part.shipment_id) for name, part in splits.items()}
    overlap = (sets["train"] & sets["validation"]) | (sets["train"] & sets["test"]) | (sets["validation"] & sets["test"])
    if overlap:
        raise ValueError(f"{len(overlap)} shipments cross split boundaries")
    return splits, {"train_end_inclusive": train_end.isoformat(), "validation_end_inclusive": val_end.isoformat()}


def make_pipeline(numeric: list[str], categorical: list[str]) -> Pipeline:
    num = Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                    ("scaler", StandardScaler())])
    cat = Pipeline([("imputer", SimpleImputer(strategy="most_frequent", keep_empty_features=True)),
                    ("onehot", OneHotEncoder(handle_unknown="ignore"))])
    prep = ColumnTransformer([("numeric", num, numeric), ("categorical", cat, categorical)], remainder="drop")
    return Pipeline([("preprocessing", prep), ("regressor", Ridge(alpha=1.0))])


def regression_metrics(actual: Any, predicted: np.ndarray) -> dict[str, float]:
    y = np.asarray(actual, dtype=float)
    yhat = np.asarray(predicted, dtype=float)
    return {"mae_hours": float(mean_absolute_error(y, yhat)),
            "rmse_hours": float(np.sqrt(mean_squared_error(y, yhat))),
            "r_squared": float(r2_score(y, yhat))}


def prediction_frame(part: pd.DataFrame, split: str, model: str, predicted: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({
        "shipment_id": part.shipment_id.to_numpy(), "eta_id": part.eta_id.to_numpy(),
        "predicted_at": part.predicted_at.astype(str).to_numpy(), "split": split, "model": model,
        "actual_remaining_hours": part[TARGET].astype(float).to_numpy(),
        "predicted_remaining_hours": np.asarray(predicted, dtype=float),
    })


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def subgroup_errors(predictions: pd.DataFrame, features: pd.DataFrame, truth_path: Path) -> pd.DataFrame:
    """Join separate ground truth after test prediction, for reporting only."""
    test = predictions[predictions.split == "test"].merge(
        features[["shipment_id", "shipment_priority"]], on="shipment_id", validate="one_to_one")
    truth = pd.read_csv(truth_path, usecols=["shipment_id", "delayed"])
    test = test.merge(truth, on="shipment_id", validate="one_to_one")
    test["delay_category"] = np.where(test.delayed.astype(int) == 1, "delayed", "on_time")
    test["error_hours"] = test.predicted_remaining_hours - test.actual_remaining_hours
    rows = []
    for dimension in ("shipment_priority", "delay_category"):
        for label, group in test.groupby(dimension, sort=True):
            rows.append({"dimension": dimension, "group": str(label), "rows": int(len(group)),
                         "mae_hours": float(mean_absolute_error(group.actual_remaining_hours, group.predicted_remaining_hours)),
                         "rmse_hours": float(np.sqrt(mean_squared_error(group.actual_remaining_hours, group.predicted_remaining_hours))),
                         "mean_error_hours_prediction_minus_actual": float(group.error_hours.mean())})
    return pd.DataFrame(rows)


def run_training(feature_dir: Path, bundle_dir: Path, model_dir: Path, report_dir: Path) -> dict[str, Any]:
    if model_dir.exists() or report_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing outputs: {model_dir}, {report_dir}")
    fp, tp = feature_dir / "features.csv", feature_dir / "targets.csv"
    dp, vp = feature_dir / "feature_dictionary.csv", feature_dir / "validation_summary.json"
    features, targets, dictionary = pd.read_csv(fp), pd.read_csv(tp), pd.read_csv(dp)
    vsummary = json.loads(vp.read_text(encoding="utf-8"))
    predictors, numeric, categorical = validate_feature_schema(
        features.columns.tolist(), targets.columns.tolist(), dictionary, vsummary)
    if features.eta_id.duplicated().any() or targets.eta_id.duplicated().any() or features.shipment_id.duplicated().any():
        raise ValueError("snapshot or shipment identifiers are duplicated")
    if set(zip(features.shipment_id, features.eta_id)) != set(zip(targets.shipment_id, targets.eta_id)):
        raise ValueError("feature/target identifier pairs do not match")
    if targets[TARGET].isna().any() or set(pd.to_numeric(targets.target_eligible).unique()) != {1}:
        raise ValueError("target is missing or includes ineligible post-prediction outcomes")
    joined = features[IDENTIFIERS + predictors].merge(
        targets[["shipment_id", "eta_id", TARGET, "target_eligible"]],
        on=["shipment_id", "eta_id"], validate="one_to_one")
    if len(joined) != len(features) or len(joined) != len(targets):
        raise ValueError("feature and target rows do not join one-to-one")
    splits, boundaries = chronological_split(joined)
    type_map = dict(zip(dictionary.loc[dictionary.role == "predictor", "name"],
                        dictionary.loc[dictionary.role == "predictor", "data_type"]))
    booleans = [n for n in predictors if type_map[n] == "boolean"]
    for name in booleans:
        values = set(pd.to_numeric(joined[name], errors="coerce").dropna().unique())
        if not values <= {0, 1}:
            raise ValueError(f"boolean predictor {name} contains non-binary values")
        joined[name] = pd.to_numeric(joined[name], errors="coerce")
    for name in numeric:
        if name not in booleans:
            joined[name] = pd.to_numeric(joined[name], errors="coerce")
    joined[categorical] = joined[categorical].astype("string").astype(object)
    splits, boundaries = chronological_split(joined)

    x_train, y_train = splits["train"][predictors], splits["train"][TARGET].astype(float)
    dummy = DummyRegressor(strategy="median").fit(x_train, y_train)
    ridge = make_pipeline(numeric, categorical).fit(x_train, y_train)
    models = {"dummy": dummy, "ridge": ridge}
    metrics: dict[str, Any] = {"units": {"mae": "hours", "rmse": "hours", "r_squared": "unitless"}, "models": {}}
    fit_predictions: dict[str, dict[str, np.ndarray]] = {}
    for name, model in models.items():
        metrics["models"][name] = {}
        fit_predictions[name] = {}
        for split_name in ("train", "validation"):
            part = splits[split_name]
            pred = model.predict(part[predictors])
            fit_predictions[name][split_name] = pred
            metrics["models"][name][split_name] = regression_metrics(part[TARGET], pred)
    selected_name = min(("dummy", "ridge"), key=lambda n: metrics["models"][n]["validation"]["mae_hours"])
    selected = models[selected_name]
    test = splits["test"]
    test_pred = selected.predict(test[predictors])
    metrics["selection"] = {"criterion": "lowest validation MAE", "selected_model": selected_name}
    metrics["selected_model_test_once"] = regression_metrics(test[TARGET], test_pred)

    split_summary: dict[str, Any] = {
        "method": "chronological predicted_at cutoffs, approximately 70/15/15; tied timestamps kept together",
        "boundaries_utc": boundaries, "shipment_overlap_count": 0, "counts": {}, "shipment_counts": {},
        "date_ranges_utc": {},
    }
    for name, part in splits.items():
        split_summary["counts"][name] = len(part)
        split_summary["shipment_counts"][name] = int(part.shipment_id.nunique())
        split_summary["date_ranges_utc"][name] = {
            "min": part.predicted_at.min().isoformat(), "max": part.predicted_at.max().isoformat()}
    frames = [prediction_frame(splits[n], n, selected_name, fit_predictions[selected_name][n])
              for n in ("train", "validation")]
    frames.append(prediction_frame(test, "test", selected_name, test_pred))
    predictions = pd.concat(frames, ignore_index=True)
    if len(predictions) != len(joined) or predictions.eta_id.duplicated().any() or not np.isfinite(predictions.predicted_remaining_hours).all():
        raise AssertionError("prediction output row, key, or finite-value check failed")

    model_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    joblib.dump(dummy, model_dir / "dummy_median.joblib")
    joblib.dump(ridge, model_dir / "ridge_pipeline.joblib")
    joblib.dump(selected, model_dir / "selected_model.joblib")
    config = {
        "source_dataset": "data/realistic_v2",
        "input_sha256": {"features.csv": _sha256(fp), "targets.csv": _sha256(tp), "feature_dictionary.csv": _sha256(dp)},
        "target": TARGET, "target_definition": "actual_delivery_time minus predicted_at in hours; target only",
        "predictor_columns": predictors, "predictor_count": len(predictors), "identifiers_excluded": IDENTIFIERS,
        "numeric_columns": numeric, "boolean_columns_treated_as_numeric_0_1": booleans,
        "categorical_columns": categorical, "ridge_alpha": 1.0, "dummy_strategy": "median",
        "numeric_preprocessing": "median imputation (keep empty fields) then StandardScaler, fit on train only",
        "categorical_preprocessing": "most-frequent imputation then OneHotEncoder, fit on train only",
        "event_availability_rule": vsummary["event_cutoff_rule"],
        "point_in_time_checks": vsummary["checks"],
        "test_usage": "test is not used in preprocessing, fitting, or selection; selected model evaluated once after validation selection",
    }
    predictions.to_csv(report_dir / "predictions.csv", index=False)
    _write_json(report_dir / "metrics.json", metrics)
    _write_json(report_dir / "split_summary.json", split_summary)
    _write_json(model_dir / "preprocessing_config.json", config)
    subgroups = subgroup_errors(predictions, features, bundle_dir / "ground_truth.csv")
    subgroups.to_csv(report_dir / "test_subgroup_errors.csv", index=False)
    _write_json(model_dir / "model_card.json", {
        "selected_model": selected_name, "target": TARGET, "dataset": "synthetic realistic_v2",
        "validation_metrics": metrics["models"][selected_name]["validation"],
        "test_metrics": metrics["selected_model_test_once"],
        "limitations": ["Synthetic-data scores do not establish real-world performance or production readiness."]})

    lines = [
        "# ETA regression — realistic_v2", "",
        "This model estimates hours from the prediction timestamp until actual delivery. Training inputs came only from data/realistic_v2.",
        "", "## Leakage controls", "",
        f"- Used {len(features):,} feature/target rows and {len(predictors)} declared predictors.",
        "- Actual delivery, target timestamps, identifiers, ground truth, and anomaly annotations are excluded from predictor inputs.",
        "- The feature bundle passed its point-in-time validation, including event_time and ingestion_time cutoffs.",
        "- Imputation, scaling, and encoding were fitted using training data only; model selection used validation MAE.",
        "", "## Chronological split", "", "| Split | Rows | Shipments | predicted_at UTC range |", "|---|---:|---:|---|",
    ]
    for name in ("train", "validation", "test"):
        r = split_summary["date_ranges_utc"][name]
        lines.append(f"| {name} | {split_summary['counts'][name]:,} | {split_summary['shipment_counts'][name]:,} | {r['min']} to {r['max']} |")
    lines += ["", f"Cutoffs: train inclusive {boundaries['train_end_inclusive']}; validation inclusive {boundaries['validation_end_inclusive']}.",
              "Shipment overlap: 0.", "", "## Metrics", "",
              "MAE and RMSE are in hours; R-squared is unitless.", "",
              "| Model | Split | MAE hours | RMSE hours | R-squared |", "|---|---|---:|---:|---:|"]
    for name in ("dummy", "ridge"):
        for split in ("train", "validation"):
            m = metrics["models"][name][split]
            lines.append(f"| {name} | {split} | {m['mae_hours']:.4f} | {m['rmse_hours']:.4f} | {m['r_squared']:.4f} |")
    m = metrics["selected_model_test_once"]
    lines += ["", f"Selected on validation MAE: {selected_name}.",
              f"Selected model test metrics, evaluated once: MAE {m['mae_hours']:.4f} hours; RMSE {m['rmse_hours']:.4f} hours; R-squared {m['r_squared']:.4f}.",
              "", "## Test subgroup errors", "",
              "Priority and on-time/delayed labels are joined from this dataset's separate ground-truth file only after test predictions. See test_subgroup_errors.csv.",
              "", "## Preprocessing", "",
              f"Numeric/boolean fields ({len(numeric)}) use median imputation and standard scaling. Categorical fields ({len(categorical)}) use most-frequent imputation and one-hot encoding. Ridge alpha=1.0.",
              "", "## Limitations", "",
              "This is a first model trained on synthetic data. Generator rules may create easier or different patterns than real logistics operations. These results do not establish real-world generalization or production readiness.", ""]
    (report_dir / "model_report.md").write_text("\n".join(lines), encoding="utf-8")
    return {"metrics": metrics, "split_summary": split_summary, "selected_model": selected_name}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-dir", type=Path, default=Path("data/realistic_v2/features"))
    parser.add_argument("--bundle-dir", type=Path, default=Path("data/realistic_v2"))
    parser.add_argument("--model-dir", type=Path, default=Path("models/realistic_v2"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports/realistic_v2"))
    args = parser.parse_args()
    print(json.dumps(run_training(args.feature_dir, args.bundle_dir, args.model_dir, args.report_dir), indent=2))


if __name__ == "__main__":
    main()
