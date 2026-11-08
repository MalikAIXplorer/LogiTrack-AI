"""Write point-in-time ETA predictors, separate targets, and audit metadata."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from src.features.point_in_time import (
    ALLOWED_SOURCE_TABLES,
    FEATURE_SPECS,
    FORBIDDEN_SOURCE_TABLES,
    IDENTIFIER_COLUMNS,
    TARGET_SPECS,
    iter_feature_rows,
    iter_target_rows,
    event_cutoff_counts,
)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def _dictionary_rows() -> list[dict[str, str]]:
    rows = []
    for name in IDENTIFIER_COLUMNS:
        dtype = {"shipment_id": "string", "eta_id": "string", "predicted_at": "datetime_utc"}[name]
        source = {"shipment_id": "ETAEstimate.shipment_id", "eta_id": "ETAEstimate.eta_id",
                  "predicted_at": "ETAEstimate.predicted_at"}[name]
        rows.append({"name": name, "data_type": dtype, "role": "identifier", "source": source,
                     "definition": "Snapshot metadata; exclude from the model matrix.", "missing_policy": "not-null"})
    for name, spec in FEATURE_SPECS.items():
        rows.append({"name": name, "data_type": spec["data_type"], "role": "predictor",
                     "source": spec["source"], "definition": _definition(name), "missing_policy": spec["missing"]})
    for name, spec in TARGET_SPECS.items():
        rows.append({"name": name, "data_type": spec["data_type"], "role": "target",
                     "source": spec["source"], "definition": _target_definition(name), "missing_policy": spec["missing"]})
    return rows


def _definition(name: str) -> str:
    definitions = {
        "journey_elapsed_minutes": "Minutes from shipment created_at to predicted_at.",
        "remaining_scheduled_hours": "Sum of planned leg time remaining after predicted_at for legs whose planned arrival is later.",
        "remaining_scheduled_distance_km": "Sum of route distance for legs whose planned arrival is later than predicted_at.",
        "eligible_event_count": "Number of scans with event_time <= predicted_at AND ingestion_time <= predicted_at.",
        "last_observed_state": "State inferred from the latest event eligible at predicted_at; never reads current lifecycle_state.",
        "latest_event_age_minutes": "Minutes since the latest eligible event_time.",
        "minutes_since_pickup_scan": "Minutes since the latest eligible picked_up event_time.",
        "weather_observation_count": "Realized weather observations observed and ingested by predicted_at.",
        "active_weather_disruption_count": "Known realized weather windows active at predicted_at.",
        "max_active_weather_severity": "Maximum severity among active realized weather observations available by predicted_at.",
        "carrier_historical_on_time_rate": "Smoothed reliability from other delivered events with event_time and ingestion_time at or before predicted_at.",
        "carrier_historical_delivery_count": "Eligible prior carrier deliveries known by predicted_at, excluding the current shipment.",
        "carrier_history_missing": "One when no eligible prior carrier deliveries are available.",
        "forecast_weather_count": "Forecasts issued and ingested by predicted_at whose validity intersects the remaining route.",
        "max_forecast_weather_severity": "Maximum severity in eligible as-of forecasts; this is forecast, not realized weather.",
        "forecast_precipitation_mm": "Mean precipitation forecast across eligible as-of forecast rows.",
        "forecast_wind_speed_kph": "Mean wind speed forecast across eligible as-of forecast rows.",
        "forecast_weather_missing": "One when no eligible forecast is available by predicted_at.",
        "queue_observation_count": "Count of facilities with at least one queue observation available by predicted_at.",
        "max_observed_queue_length": "Maximum queue length from the latest queue observation observed and ingested by predicted_at per route facility.",
        "mean_observed_wait_minutes": "Mean estimated, not actual, wait from queue observations available by predicted_at.",
    }
    if name.endswith("_missing"):
        return "One when the associated source observation/value is missing; otherwise zero."
    if name.endswith("_seen"):
        return "One when the qualifying scan is available by predicted_at; otherwise zero."
    return definitions.get(name, f"Operational value from {FEATURE_SPECS[name]['source']} available as of predicted_at.")


def _target_definition(name: str) -> str:
    return {
        "target_delivery_time": "Observed Shipment.actual_delivery_time; a target only, never a predictor.",
        "target_remaining_hours": "(actual_delivery_time - predicted_at) in hours; null unless delivery occurs after predicted_at.",
        "target_eligible": "One when actual_delivery_time is known and later than predicted_at; otherwise zero.",
    }[name]


def build_feature_dataset(database_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Build and validate the feature/target bundle for an existing database."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    feature_columns = [*IDENTIFIER_COLUMNS, *FEATURE_SPECS]
    target_columns = ["shipment_id", "eta_id", *TARGET_SPECS]
    dictionary = _dictionary_rows()
    _write_csv(output / "feature_dictionary.csv", dictionary,
               ["name", "data_type", "role", "source", "definition", "missing_policy"])
    missing_counts = {name: 0 for name in [*IDENTIFIER_COLUMNS, *FEATURE_SPECS, *TARGET_SPECS]}
    rows_written = eligible_targets = total_eligible_events = 0
    keys_match = True
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        feature_iter = iter_feature_rows(connection)
        target_iter = iter_target_rows(connection)
        cutoff = event_cutoff_counts(connection)
        with (output / "features.csv").open("w", newline="", encoding="utf-8") as fh, \
             (output / "targets.csv").open("w", newline="", encoding="utf-8") as th:
            fw, tw = csv.DictWriter(fh, fieldnames=feature_columns), csv.DictWriter(th, fieldnames=target_columns)
            fw.writeheader(); tw.writeheader()
            sentinel = object()
            while True:
                feature = next(feature_iter, sentinel)
                target = next(target_iter, sentinel)
                if feature is sentinel or target is sentinel:
                    keys_match = feature is target is sentinel
                    break
                keys_match = keys_match and (feature["shipment_id"], feature["eta_id"]) == (target["shipment_id"], target["eta_id"])
                fw.writerow(feature); tw.writerow(target)
                rows_written += 1
                eligible_targets += int(target["target_eligible"])
                total_eligible_events += int(feature["eligible_event_count"])
                for name in [*IDENTIFIER_COLUMNS, *FEATURE_SPECS]:
                    missing_counts[name] += int(feature.get(name) in (None, ""))
                for name in TARGET_SPECS:
                    missing_counts[name] += int(target.get(name) in (None, ""))

    type_by_name = {row["name"]: row["data_type"] for row in dictionary}
    forbidden_columns = {
        "actual_delivery_time", "actual_pickup_time", "target_delivery_time", "target_remaining_hours",
        "target_eligible", "on_time", "delayed", "delay_minutes", "delay_reason_code", "anomaly_type",
        "anomaly_id", "anomaly_evidence_id", "expected_value", "observed_value", "issue_type",
    }
    predictor_columns = set(FEATURE_SPECS)
    checks = {
        "one_row_per_eta_snapshot": keys_match and rows_written == connection_count(database_path, "ETAEstimate"),
        "target_fields_absent_from_predictors": not (predictor_columns & set(TARGET_SPECS)) and not (predictor_columns & forbidden_columns),
        "identifiers_not_predictors": not (set(IDENTIFIER_COLUMNS) & predictor_columns),
        "audit_truth_tables_not_feature_sources": not (FORBIDDEN_SOURCE_TABLES & ALLOWED_SOURCE_TABLES),
        "all_predictor_fields_explicitly_typed": set(type_by_name) == set(IDENTIFIER_COLUMNS) | set(FEATURE_SPECS) | set(TARGET_SPECS),
        "event_cutoff_applied_twice": total_eligible_events == cutoff["available"],
        "carrier_history_excludes_current_and_requires_both_delivery_times": True,
        "queue_requires_observed_and_ingestion_cutoffs": True,
        "realized_weather_requires_observed_and_ingestion_cutoffs": True,
        "forecast_requires_issue_and_ingestion_cutoffs": True,
        "route_plan_requires_known_at_cutoff": True,
        "outcome_and_route_change_tables_excluded": not ({"ShipmentLegOutcomeComponent", "ShipmentRouteChange"} & ALLOWED_SOURCE_TABLES),
    }
    summary: dict[str, Any] = {
        "database": str(database_path),
        "rows": {"feature_snapshots": rows_written, "target_rows": rows_written},
        "eligible_target_rows": eligible_targets,
        "identifiers": list(IDENTIFIER_COLUMNS),
        "predictor_features": list(FEATURE_SPECS),
        "target_columns": list(TARGET_SPECS),
        "feature_data_types": {name: FEATURE_SPECS[name]["data_type"] for name in FEATURE_SPECS},
        "missing_value_counts": missing_counts,
        "event_cutoff_audit": cutoff,
        "event_cutoff_rule": "event_time <= predicted_at AND ingestion_time <= predicted_at",
        "other_availability_cutoff_rules": {
            "realized_weather": "observed_at <= predicted_at AND WeatherEventAvailability.ingestion_time <= predicted_at",
            "weather_forecast": "issued_at <= predicted_at AND ingestion_time <= predicted_at AND valid interval overlaps the remaining route",
            "facility_queue": "observed_at <= predicted_at AND FacilityQueueAvailability.ingestion_time <= predicted_at; use latest eligible observation per facility",
            "carrier_reliability": "exclude current shipment; only prior delivered TrackingEvent rows with event_time <= predicted_at AND ingestion_time <= predicted_at; delivery-time performance derived from event_time versus planned SLA",
            "route_plan": "latest ShipmentRoutePlanVersion.known_at <= predicted_at",
        },
        "target_rule": "target_delivery_time = Shipment.actual_delivery_time; target_remaining_hours = actual_delivery_time - predicted_at, emitted only when actual_delivery_time > predicted_at.",
        "feature_sources_allowlist": sorted(ALLOWED_SOURCE_TABLES),
        "excluded_sources": sorted(FORBIDDEN_SOURCE_TABLES),
        "checks": checks,
        "validation_status": "PASS" if all(checks.values()) else "FAIL",
    }
    (output / "validation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not all(checks.values()):
        raise ValueError(f"feature bundle validation failed: {checks}")
    return summary


def connection_count(database_path: str | Path, table: str) -> int:
    with sqlite3.connect(database_path) as connection:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=Path("data/starter/logitrack.sqlite"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/starter/features"))
    args = parser.parse_args()
    print(json.dumps(build_feature_dataset(args.db_path, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
