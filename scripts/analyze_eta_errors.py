"""Interpret saved ETA model and analyze its already-saved test predictions.

This script loads artifacts only. It never calls fit/train and never changes source data.
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FEATURE_DIR = ROOT / "data/full/features"
MODEL_DIR = ROOT / "models/eta_v1"
MODEL_REPORT_DIR = ROOT / "reports/modeling/eta_v1"
OUT = ROOT / "reports/model_analysis"


def grouped_error(frame: pd.DataFrame, by: str) -> pd.DataFrame:
    rows = []
    for value, group in frame.groupby(by, dropna=False, observed=True):
        error = group.predicted_remaining_hours - group.target_remaining_hours
        absolute = error.abs()
        rows.append({
            "group": "missing" if pd.isna(value) else str(value),
            "n": len(group),
            "mean_error_hours_positive_means_overprediction": error.mean(),
            "median_error_hours": error.median(),
            "mae_hours": absolute.mean(),
            "median_absolute_error_hours": absolute.median(),
            "p90_absolute_error_hours": absolute.quantile(.90),
            "within_1h_pct": 100 * (absolute <= 1).mean(),
            "within_2h_pct": 100 * (absolute <= 2).mean(),
            "within_4h_pct": 100 * (absolute <= 4).mean(),
        })
    return pd.DataFrame(rows).sort_values("group").reset_index(drop=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    model = joblib.load(MODEL_DIR / "ridge_pipeline.joblib")
    selected = joblib.load(MODEL_DIR / "selected_model.joblib")
    config = json.loads((MODEL_REPORT_DIR / "preprocessing_config.json").read_text(encoding="utf-8"))
    predictions = pd.read_csv(MODEL_REPORT_DIR / "predictions.csv")
    features = pd.read_csv(FEATURE_DIR / "features.csv")
    targets = pd.read_csv(FEATURE_DIR / "targets.csv")

    test = predictions[predictions.split == "test"].copy()
    if len(test) != 7_500 or test.shipment_id.nunique() != 7_500:
        raise ValueError("Saved test predictions do not contain exactly 7,500 distinct shipments")
    if test.model.nunique() != 1 or test.model.iloc[0] != "ridge":
        raise ValueError("Expected the saved selected Ridge test predictions")
    # Re-inference with the already-fitted artifact checks prediction provenance; this is not fitting.
    source = features.merge(targets[["shipment_id", "eta_id", "target_remaining_hours", "target_delivery_time"]],
                            on=["shipment_id", "eta_id"], validate="one_to_one")
    source = source.merge(test[["shipment_id", "eta_id", "predicted_remaining_hours"]],
                          on=["shipment_id", "eta_id"], validate="one_to_one")
    predictor_columns = config["predictor_columns"]
    check_prediction = model.predict(source[predictor_columns])
    prediction_reproduction_max_abs_difference = float(np.max(np.abs(check_prediction - source.predicted_remaining_hours.to_numpy())))
    if prediction_reproduction_max_abs_difference > 1e-9:
        raise ValueError("Saved pipeline predictions do not reproduce saved test predictions")

    source["predicted_at"] = pd.to_datetime(source.predicted_at, utc=True)
    source["target_delivery_time"] = pd.to_datetime(source.target_delivery_time, utc=True)
    source["predicted_delivery_time"] = source.predicted_at + pd.to_timedelta(source.predicted_remaining_hours, unit="h")
    source["error_hours"] = source.predicted_remaining_hours - source.target_remaining_hours
    source["absolute_error_hours"] = source.error_hours.abs()
    # Add interpretable groupings from available operational fields.
    source["weather_exposure"] = np.where(source.active_weather_disruption_count.gt(0), "active_disruption", "no_active_disruption")
    source["remaining_distance_quartile"] = pd.qcut(
        source.remaining_scheduled_distance_km.rank(method="first"), 4,
        labels=["Q1 shortest", "Q2", "Q3", "Q4 longest"],
    )

    # Coefficients are read from the already-fitted transformed Ridge model.
    preprocessor = model.named_steps["preprocessing"]
    regressor = model.named_steps["regressor"]
    transformed_names = preprocessor.get_feature_names_out()
    coefficients = pd.DataFrame({"encoded_feature": transformed_names, "coefficient": regressor.coef_})
    coefficients["absolute_coefficient"] = coefficients.coefficient.abs()
    coefficients["feature_family"] = np.where(coefficients.encoded_feature.str.startswith("numeric__"), "scaled numeric", "one-hot category")
    coefficients["interpretation"] = np.where(
        coefficients.feature_family.eq("scaled numeric"),
        "hours per one training-standard-deviation increase, other inputs held fixed",
        "hours contribution for this encoded category indicator, conditional on other inputs",
    )
    top15 = coefficients.sort_values("absolute_coefficient", ascending=False).head(15).copy()
    top15.to_csv(OUT / "top_ridge_coefficients.csv", index=False)
    coefficients.sort_values("absolute_coefficient", ascending=False).to_csv(OUT / "all_ridge_coefficients.csv", index=False)

    groups: dict[str, pd.DataFrame] = {}
    for label, column in [
        ("priority", "shipment_priority"), ("carrier_type", "carrier_type"),
        ("service_mode", "service_mode"), ("weather_exposure", "weather_exposure"),
        ("remaining_distance_quartile", "remaining_distance_quartile"),
    ]:
        grouped = grouped_error(source, column)
        groups[label] = grouped
        grouped.to_csv(OUT / f"errors_by_{label}.csv", index=False)

    absolute = source.absolute_error_hours
    signed = source.error_hours
    residual_summary = {
        "test_rows": len(source),
        "error_definition": "predicted_remaining_hours - target_remaining_hours; positive means ETA predicts later delivery than observed (overprediction)",
        "mean_error_hours_prediction_bias": float(signed.mean()),
        "median_error_hours": float(signed.median()),
        "mean_absolute_error_hours": float(absolute.mean()),
        "median_absolute_error_hours": float(absolute.median()),
        "signed_error_percentiles_hours": {str(p): float(signed.quantile(p / 100)) for p in [1, 5, 10, 25, 50, 75, 90, 95, 99]},
        "absolute_error_percentiles_hours": {str(p): float(absolute.quantile(p / 100)) for p in [50, 75, 90, 95, 99]},
        "maximum_absolute_error_hours": float(absolute.max()),
        "maximum_overprediction_hours": float(signed.max()),
        "maximum_underprediction_hours": float(signed.min()),
        "within_threshold_pct": {str(hours): 100 * float((absolute <= hours).mean()) for hours in [1, 2, 4, 6]},
        "saved_model_reproduction_max_abs_difference_hours": prediction_reproduction_max_abs_difference,
    }
    (OUT / "residual_summary.json").write_text(json.dumps(residual_summary, indent=2) + "\n", encoding="utf-8")

    example_cols = ["shipment_id", "eta_id", "shipment_priority", "carrier_type", "service_mode", "predicted_at",
                    "target_delivery_time", "predicted_delivery_time", "target_remaining_hours",
                    "predicted_remaining_hours", "error_hours", "absolute_error_hours"]
    good = source.nsmallest(10, "absolute_error_hours")[example_cols].copy()
    good.insert(0, "example_group", "10 lowest absolute errors")
    poor = source.nlargest(10, "absolute_error_hours")[example_cols].copy()
    poor.insert(0, "example_group", "10 highest absolute errors")
    examples = pd.concat([good, poor], ignore_index=True)
    for col in ["predicted_at", "target_delivery_time", "predicted_delivery_time"]:
        examples[col] = examples[col].astype(str)
    examples.to_csv(OUT / "test_shipment_examples.csv", index=False)

    plt.figure(figsize=(7, 6))
    plt.scatter(source.target_remaining_hours, source.predicted_remaining_hours, s=9, alpha=.28)
    low = min(source.target_remaining_hours.min(), source.predicted_remaining_hours.min())
    high = max(source.target_remaining_hours.max(), source.predicted_remaining_hours.max())
    plt.plot([low, high], [low, high], "r--", linewidth=1)
    plt.xlabel("Actual remaining time (hours)")
    plt.ylabel("Predicted remaining time (hours)")
    plt.title("Test set: actual vs predicted remaining time")
    plt.tight_layout()
    plt.savefig(OUT / "actual_vs_predicted.png", dpi=150)
    plt.close()

    plt.figure(figsize=(8, 5))
    plt.hist(source.error_hours, bins=50, color="#4472C4", edgecolor="white")
    plt.axvline(0, color="black", linestyle="--", linewidth=1)
    plt.axvline(source.error_hours.mean(), color="#C00000", linestyle="-", label=f"Mean error {source.error_hours.mean():.2f} h")
    plt.xlabel("Prediction error (predicted − actual, hours)")
    plt.ylabel("Test shipments")
    plt.title("Test residual distribution")
    plt.legend()
    plt.tight_layout()
    plt.savefig(OUT / "residual_distribution.png", dpi=150)
    plt.close()

    plt.figure(figsize=(8, 5))
    plt.scatter(source.predicted_remaining_hours, source.error_hours, s=9, alpha=.28)
    plt.axhline(0, color="black", linestyle="--", linewidth=1)
    plt.xlabel("Predicted remaining time (hours)")
    plt.ylabel("Prediction error (hours; positive = overprediction)")
    plt.title("Test residuals vs predicted remaining time")
    plt.tight_layout()
    plt.savefig(OUT / "residuals_vs_prediction.png", dpi=150)
    plt.close()

    error_by_group = []
    for label, table in groups.items():
        eligible = table[table.n >= 100].copy()
        eligible["analysis_group"] = label
        error_by_group.append(eligible)
    if error_by_group:
        pd.concat(error_by_group, ignore_index=True).to_csv(OUT / "systematic_group_bias.csv", index=False)

    # Collect exact lineage facts for the human-readable interpretation.
    eta_gen = (ROOT / "src/generator/generate.py").read_text(encoding="utf-8")
    feature_builder = (ROOT / "src/features/point_in_time.py").read_text(encoding="utf-8")
    report = [
        "# ETA model interpretation and test error analysis", "",
        "## Scope and integrity", "",
        f"This analysis reads the fitted Ridge pipeline at `models/eta_v1/ridge_pipeline.joblib` and its saved prediction file. No model fitting or selection was run. The saved artifact reproduced the saved test predictions with maximum absolute difference {prediction_reproduction_max_abs_difference:.12g} hours.",
        "The test set remains the original 7,500 shipment holdout; this analysis uses it only to describe errors after model selection. No source feature, target, or prediction files were edited.", "",
        "## Reading Ridge coefficients", "",
        "For numeric inputs, the pipeline standardizes each value, so a coefficient is the estimated change in remaining hours for a one-training-standard-deviation increase in that input, holding other inputs fixed. For one-hot categories, a coefficient is the contribution of that encoded category indicator in hours. Because the encoder keeps every category and Ridge regularizes coefficients, category values should be read as adjusted associations around the fitted intercept, not as a causal comparison against a single dropped reference category.",
        "Correlated predictors make individual coefficients less stable: the route distance/hours and elapsed-time fields contain overlapping information. Therefore the ranking below is influence within this fitted model, not proof that a factor causes delay.", "",
        "### 15 largest absolute coefficients", "",
        "| Encoded feature | Type | Coefficient | Interpretation |", "|---|---|---:|---|",
    ]
    for _, row in top15.iterrows():
        report.append(f"| `{row.encoded_feature}` | {row.feature_family} | {row.coefficient:+.4f} | {row.interpretation} |")
    report += [
        "", "Positive coefficients push the model toward more hours remaining; negative coefficients push it toward fewer hours, all else held fixed. Numeric coefficients are per standard deviation because the numeric pipeline scales inputs.", "",
        "## Test residuals", "",
        f"Residual/error is defined as predicted minus actual remaining hours, so positive bias means the model predicts a later delivery than occurred (overprediction). Mean error is {signed.mean():+.4f} h, median error {signed.median():+.4f} h, mean absolute error {absolute.mean():.4f} h, and median absolute error {absolute.median():.4f} h.",
        f"Signed error percentiles (hours): {', '.join(f'P{p} {signed.quantile(p/100):+.3f}' for p in [1,5,10,25,50,75,90,95,99])}.",
        f"Absolute error percentiles (hours): {', '.join(f'P{p} {absolute.quantile(p/100):.3f}' for p in [50,75,90,95,99])}; maximum absolute error {absolute.max():.3f} h. Largest overprediction {signed.max():+.3f} h; largest underprediction {signed.min():+.3f} h.", "",
        "### Actual and predicted delivery-time examples", "",
        "The CSV contains ten best and ten worst test predictions. Actual delivery time is from the separate target file; predicted delivery time is `predicted_at + predicted_remaining_hours`.",
        "| Group | Shipment ID | Priority | Carrier | Mode | Actual delivery (UTC) | Predicted delivery (UTC) | Error (h) |", "|---|---|---|---|---|---|---|---:|",
    ]
    for _, row in examples.iterrows():
        report.append(f"| {row.example_group} | `{row.shipment_id}` | {row.shipment_priority} | {row.carrier_type} | {row.service_mode} | {row.target_delivery_time} | {row.predicted_delivery_time} | {row.error_hours:+.3f} |")
    report += ["", "### Errors by operational group", ""]
    group_titles = {
        "priority": "Shipment priority", "carrier_type": "Carrier type", "service_mode": "Service mode",
        "weather_exposure": "Weather exposure (active disruption present at prediction)",
        "remaining_distance_quartile": "Remaining planned route distance quartile",
    }
    for label, table in groups.items():
        report += [f"#### {group_titles[label]}", "", "Positive mean error indicates overprediction. These are descriptive groups; small differences are not significance tests.",
                   "", "| Group | N | Mean error (h) | MAE (h) | Median absolute (h) | P90 absolute (h) | Within 1h | Within 2h | Within 4h |",
                   "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for _, row in table.iterrows():
            report.append(f"| {row.group} | {int(row.n):,} | {row.mean_error_hours_positive_means_overprediction:+.3f} | {row.mae_hours:.3f} | {row.median_absolute_error_hours:.3f} | {row.p90_absolute_error_hours:.3f} | {row.within_1h_pct:.1f}% | {row.within_2h_pct:.1f}% | {row.within_4h_pct:.1f}% |")
        report.append("")
    bias_groups = []
    for label, table in groups.items():
        for _, row in table[table.n >= 100].iterrows():
            if abs(row.mean_error_hours_positive_means_overprediction) >= 1:
                bias_groups.append((label, row.group, row.n, row.mean_error_hours_positive_means_overprediction))
    report += ["### Systematic bias observations", ""]
    if bias_groups:
        for label, value, n, bias in bias_groups:
            direction = "overpredicts" if bias > 0 else "underpredicts"
            report.append(f"- In {label} group `{value}` (n={n:,}), mean error is {bias:+.3f} h; this group {direction} by at least one hour on average.")
    else:
        report.append("- No category/distance/weather group with at least 100 test rows has an absolute mean signed error of one hour or more.")
    report += [
        "", "## Why performance may be unusually high", "",
        "The generator code makes actual delivery time a deterministic operational construction with a small stable per-shipment random component: planned delivery plus a schedule variance, with fixed delay-minute mechanisms for delay reasons. It creates route duration and ETA time from the same planned route schedule. The feature builder derives remaining scheduled hours/distance, route duration, and journey elapsed time from that schedule. Consequently, those predictors closely reconstruct the target's planned-time component; this is a synthetic-data relationship rather than evidence of real-world causal or predictive strength.",
        "The target itself is computed in `src/features/point_in_time.py` as actual delivery time minus predicted_at and is kept separate from predictors. No target or audit fields were supplied to the model. Even without direct leakage, generator construction can make the task much easier than real operations. The model should be tested on independent, real historical shipments before operational use.", "",
        "## Practical error thresholds to consider", "",
        "No business SLA or dispatch decision threshold was supplied, so these are evaluation bands, not requirements: within 1 hour could support near-term ETA confidence; 2 hours may be a useful planning band; 4 hours may suit broader customer communication; above 4 hours could trigger a manual review or lower confidence. The saved group tables show the observed share of test rows within each band. Thresholds should be selected with operations teams based on decisions, costs, and customer promises.", "",
        "## Figures", "",
        "- ![Actual versus predicted remaining time](actual_vs_predicted.png)",
        "- ![Test residual distribution](residual_distribution.png)",
        "- ![Residuals versus prediction](residuals_vs_prediction.png)", "",
        "## Source logic inspected", "",
        "- `src/generator/generate.py`: constructs planned and actual delivery timestamps, schedule variance, delivery events, and ETA snapshots.",
        "- `src/features/point_in_time.py`: builds predictor values at the ETA cutoff and separately derives `target_remaining_hours` from actual delivery.",
        "- `reports/modeling/eta_v1/preprocessing_config.json`: documents the actual fitted predictor set and transformations.", "",
    ]
    (OUT / "model_interpretation_report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({"output_dir": str(OUT), "residual_summary": residual_summary,
                      "top_coefficients": top15[["encoded_feature", "coefficient", "feature_family"]].to_dict("records"),
                      "group_tables": {k: v.to_dict("records") for k, v in groups.items()}}, indent=2))


if __name__ == "__main__":
    main()
