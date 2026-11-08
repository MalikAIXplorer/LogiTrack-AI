import csv
import json
import shutil
import sqlite3
from datetime import datetime, timedelta

from src.features.build_features import build_feature_dataset
from src.features.point_in_time import FEATURE_SPECS, build_feature_rows, build_target_rows

STARTER_DB = "data/starter/logitrack.sqlite"


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_starter_feature_bundle_and_cutoff_counts(tmp_path):
    summary = build_feature_dataset(STARTER_DB, tmp_path)
    features = _read_csv(tmp_path / "features.csv")
    targets = _read_csv(tmp_path / "targets.csv")
    dictionary = _read_csv(tmp_path / "feature_dictionary.csv")
    assert len(features) == len(targets) == 100
    assert len({row["eta_id"] for row in features}) == 100
    assert summary["event_cutoff_audit"] == {
        "total": 600,
        "available": 289,
        "occurred_but_ingested_late": 18,
        "event_after_prediction": 293,
        "ingested_after_prediction": 311,
    }
    assert sum(int(row["eligible_event_count"]) for row in features) == 289
    assert summary["validation_status"] == "PASS"
    assert {row["role"] for row in dictionary} == {"identifier", "predictor", "target"}
    assert set(FEATURE_SPECS) <= {row["name"] for row in dictionary if row["role"] == "predictor"}
    saved = json.loads((tmp_path / "validation_summary.json").read_text(encoding="utf-8"))
    assert saved["rows"] == {"feature_snapshots": 100, "target_rows": 100}


def test_late_and_future_events_are_excluded_by_both_time_cutoffs(tmp_path):
    db_path = tmp_path / "cutoff.sqlite"
    shutil.copyfile(STARTER_DB, db_path)
    baseline_dir = tmp_path / "baseline"
    baseline = build_feature_dataset(db_path, baseline_dir)
    baseline_rows = {r["eta_id"]: r for r in _read_csv(baseline_dir / "features.csv")}

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        eta_id, shipment_id, predicted_at = connection.execute(
            "SELECT eta_id,shipment_id,predicted_at FROM ETAEstimate WHERE shipment_id='SHP-2026-000001'"
        ).fetchone()
        leg_id, facility_id = connection.execute(
            "SELECT leg_id,origin_facility_id FROM ShipmentLeg WHERE shipment_id=? ORDER BY leg_number LIMIT 1",
            (shipment_id,),
        ).fetchone()
        cutoff = datetime.fromisoformat(predicted_at.replace("Z", "+00:00"))
        cases = (
            ("EVT-SHP-2026-000001-000007", cutoff - timedelta(minutes=5), cutoff + timedelta(minutes=5)),
            ("EVT-SHP-2026-000001-000008", cutoff + timedelta(minutes=5), cutoff + timedelta(minutes=10)),
        )
        for event_id, event_time, ingestion_time in cases:
            connection.execute("""INSERT INTO TrackingEvent
                (event_id,shipment_id,leg_id,facility_id,event_type,event_time,ingestion_time,source)
                VALUES (?,?,?,?,?,?,?,?)""", (event_id, shipment_id, leg_id, facility_id, "delay",
                event_time.isoformat().replace("+00:00", "Z"), ingestion_time.isoformat().replace("+00:00", "Z"), "system"))
        connection.commit()

    changed_dir = tmp_path / "with_unavailable_events"
    changed = build_feature_dataset(db_path, changed_dir)
    changed_rows = {r["eta_id"]: r for r in _read_csv(changed_dir / "features.csv")}
    assert int(baseline_rows[eta_id]["eligible_event_count"]) == 3
    assert changed_rows[eta_id]["eligible_event_count"] == baseline_rows[eta_id]["eligible_event_count"]
    assert changed_rows[eta_id]["latest_event_type"] == baseline_rows[eta_id]["latest_event_type"]
    assert changed["event_cutoff_audit"]["occurred_but_ingested_late"] == 19
    assert changed["event_cutoff_audit"]["event_after_prediction"] == 294


def test_outcomes_and_anomaly_evidence_cannot_change_predictor_rows(tmp_path):
    base_features = tmp_path / "base_features"
    build_feature_dataset(STARTER_DB, base_features)

    altered_db = tmp_path / "outcomes_altered.sqlite"
    shutil.copyfile(STARTER_DB, altered_db)
    with (tmp_path / "ground_truth.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("shipment_id", "actual_delivery_time", "on_time", "delayed", "anomaly_type"))
        writer.writerow(("SHP-2026-000001", "2020-01-01T00:00:00Z", "0", "1", "poisoned"))
    with sqlite3.connect(altered_db) as connection:
        connection.execute("""UPDATE Shipment SET actual_delivery_time='2027-01-01T00:00:00Z'
            WHERE shipment_id='SHP-2026-000001'""")
        connection.execute("UPDATE AnomalyAudit SET observed_value='POISONED_AUDIT_VALUE'")
        connection.execute("UPDATE AnomalyETAInputEvidence SET feature_value='POISONED_EVIDENCE_VALUE'")
        connection.commit()
    altered_output = tmp_path / "altered_output"
    build_feature_dataset(altered_db, altered_output)

    assert (base_features / "features.csv").read_bytes() == (altered_output / "features.csv").read_bytes()
    assert (base_features / "targets.csv").read_bytes() != (altered_output / "targets.csv").read_bytes()
    feature_headers = set(next(csv.reader((base_features / "features.csv").open(encoding="utf-8"))))
    assert "actual_delivery_time" not in feature_headers
    assert "target_delivery_time" not in feature_headers
    assert not feature_headers & {"on_time", "delayed", "anomaly_type", "anomaly_id", "observed_value"}


def test_missing_operational_values_are_null_with_explicit_flags(tmp_path):
    db_path = tmp_path / "missing.sqlite"
    shutil.copyfile(STARTER_DB, db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE Shipment SET weight_kg=NULL WHERE shipment_id='SHP-2026-000001'")
        connection.execute("""UPDATE FacilityQueue SET estimated_wait_minutes=NULL WHERE facility_id IN (
            SELECT origin_facility_id FROM ShipmentLeg WHERE shipment_id='SHP-2026-000001'
            UNION SELECT destination_facility_id FROM ShipmentLeg WHERE shipment_id='SHP-2026-000001'
        )""")
        connection.commit()
    output = tmp_path / "missing_output"
    summary = build_feature_dataset(db_path, output)
    first = next(r for r in _read_csv(output / "features.csv") if r["shipment_id"] == "SHP-2026-000001")
    assert first["shipment_weight_kg"] == ""
    assert first["weight_missing"] == "1"
    assert first["mean_observed_wait_minutes"] == ""
    assert first["queue_wait_missing"] == "1"
    assert first["queue_observation_missing"] == "0"
    assert summary["missing_value_counts"]["shipment_weight_kg"] >= 1

    with sqlite3.connect(db_path) as connection:
        connection.execute("""DELETE FROM FacilityQueue WHERE facility_id IN (
            SELECT origin_facility_id FROM ShipmentLeg WHERE shipment_id='SHP-2026-000001'
            UNION SELECT destination_facility_id FROM ShipmentLeg WHERE shipment_id='SHP-2026-000001'
        )""")
        connection.commit()
    no_queue_output = tmp_path / "no_queue_output"
    build_feature_dataset(db_path, no_queue_output)
    no_queue = next(r for r in _read_csv(no_queue_output / "features.csv") if r["shipment_id"] == "SHP-2026-000001")
    assert no_queue["max_observed_queue_length"] == ""
    assert no_queue["queue_observation_missing"] == "1"


def test_incremental_feature_output_matches_row_builders(tmp_path):
    with sqlite3.connect(STARTER_DB) as connection:
        expected_features = build_feature_rows(connection)
        expected_targets = build_target_rows(connection)
    build_feature_dataset(STARTER_DB, tmp_path)
    assert _read_csv(tmp_path / "features.csv") == [
        {key: "" if value is None else str(value) for key, value in row.items()}
        for row in expected_features
    ]
    assert _read_csv(tmp_path / "targets.csv") == [
        {key: "" if value is None else str(value) for key, value in row.items()}
        for row in expected_targets
    ]
