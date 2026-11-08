import csv
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random

import pandas as pd
import pytest

from src.features.build_features import build_feature_dataset
from src.generator.generate import GenerationConfig, generate_dataset
from src.generator.realistic_v2 import (
    _carrier_delay_minutes,
    _detour_extra_minutes,
    _queue_wait_minutes,
    _weather_delay,
)
from src.validation.dataset import _delivery_label_mismatches, validate_dataset


@pytest.fixture(scope="module")
def realistic_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("realistic_v2") / "bundle"
    config = GenerationConfig(200, 20, 40, 1200, 20261022, True)
    summary = generate_dataset(config, root, batch_size=50)
    assert summary["profile"] == "realistic_v2"
    return root


def _csv_row(path: Path, key: str, value: str) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as handle:
        return next(row for row in csv.DictReader(handle) if row[key] == value)


def _copy_database(bundle: Path, destination: Path) -> None:
    shutil.copy2(bundle / "logitrack.sqlite", destination)


def test_priority_categories_have_both_labels_and_labels_reconcile(realistic_bundle):
    shipments = pd.read_csv(realistic_bundle / "shipment.csv")
    truth = pd.read_csv(realistic_bundle / "ground_truth.csv")
    service = pd.read_csv(realistic_bundle / "servicelevel.csv")
    merged = truth[["shipment_id", "delayed", "on_time"]].merge(
        shipments[["shipment_id", "priority", "planned_delivery_time", "actual_delivery_time", "service_level_id"]],
        on="shipment_id",
    )
    merged = merged.merge(service[["service_level_id", "on_time_tolerance_minutes"]], on="service_level_id")
    assert set(merged.priority) == {"low", "standard", "high", "critical"}
    for _, group in merged.groupby("priority"):
        assert set(group.delayed.astype(int)) == {0, 1}
        assert set(group.on_time.astype(int)) == {0, 1}
    assert (merged.delayed.astype(int) + merged.on_time.astype(int) == 1).all()
    components = pd.read_csv(realistic_bundle / "shipmentlegoutcomecomponent.csv")
    assert {"travel_variation", "carrier_reliability", "facility_queue", "customs"} <= set(components.cause)
    actual = pd.to_datetime(merged.actual_delivery_time, utc=True)
    planned = pd.to_datetime(merged.planned_delivery_time, utc=True)
    expected_delayed = actual > planned + pd.to_timedelta(merged.on_time_tolerance_minutes, unit="m")
    assert (expected_delayed.astype(int).to_numpy() == merged.delayed.astype(int).to_numpy()).all()


def test_weather_queue_carrier_and_detour_effect_formulas():
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)
    intersecting = [{"route_id": "R1", "valid_from": "2026-02-01T01:00:00Z",
                     "valid_to": "2026-02-01T02:00:00Z", "condition": "storm", "severity": 5,
                     "weather_event_id": "W1"}]
    assert _weather_delay("R2", start, end, intersecting) == (0, None)
    assert _weather_delay("R1", end, end + timedelta(hours=1), intersecting) == (0, None)
    impact, event = _weather_delay("R1", start, end, intersecting)
    assert impact > 0 and event["weather_event_id"] == "W1"

    assert _queue_wait_minutes(0, "standard", random.Random(7)) == 0
    assert _queue_wait_minutes(200, "standard", random.Random(7)) > 0
    high_reliability = _carrier_delay_minutes(.99, random.Random(11))
    low_reliability = _carrier_delay_minutes(.75, random.Random(11))
    assert low_reliability > high_reliability
    assert _detour_extra_minutes(5.0, 6.5, 30) == 120
    assert _detour_extra_minutes(5.0, 4.0, 30) == 30


def test_delivery_label_validator_handles_exact_tolerance_boundary():
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE gt(shipment_id TEXT, delayed INTEGER, on_time INTEGER, delay_minutes INTEGER);
            CREATE TABLE Shipment(shipment_id TEXT, service_level_id TEXT, planned_delivery_time TEXT, actual_delivery_time TEXT);
            CREATE TABLE ServiceLevel(service_level_id TEXT, on_time_tolerance_minutes INTEGER);
        """)
        connection.execute("INSERT INTO ServiceLevel VALUES('SLV-TEST',60)")
        connection.executemany("INSERT INTO Shipment VALUES(?,?,?,?)", [
            ("SHP-ON-BOUNDARY", "SLV-TEST", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            ("SHP-OVER-BOUNDARY", "SLV-TEST", "2026-01-01T00:00:00Z", "2026-01-01T01:01:00Z"),
        ])
        connection.executemany("INSERT INTO gt VALUES(?,?,?,?)", [
            ("SHP-ON-BOUNDARY", 0, 1, 60),
            ("SHP-OVER-BOUNDARY", 1, 0, 61),
        ])
        assert _delivery_label_mismatches(connection) == []


def test_future_weather_queue_carrier_and_route_records_do_not_change_earlier_features(realistic_bundle, tmp_path):
    source_features = pd.read_csv(realistic_bundle / "features/features.csv")
    sample = source_features.iloc[80]
    eta_id, shipment_id, cutoff_text = sample.eta_id, sample.shipment_id, sample.predicted_at
    cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
    changed_db = tmp_path / "future_inputs.sqlite"
    _copy_database(realistic_bundle, changed_db)
    with sqlite3.connect(changed_db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        leg_id, facility_id, route_id = connection.execute("""SELECT leg_id,origin_facility_id,route_id FROM ShipmentLeg
            WHERE shipment_id=? ORDER BY leg_number LIMIT 1""", (shipment_id,)).fetchone()
        future = cutoff + timedelta(hours=2)
        future_end = future + timedelta(hours=8)
        connection.execute("""INSERT INTO WeatherEvent(weather_event_id,facility_id,route_id,observed_at,valid_from,valid_to,
            condition,severity,precipitation_mm,wind_speed_kph,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("WTH-FUTURE-TEST", None, route_id, future.isoformat().replace("+00:00", "Z"), future.isoformat().replace("+00:00", "Z"),
             future_end.isoformat().replace("+00:00", "Z"), "storm", 5, 80, 100, "test"))
        connection.execute("INSERT INTO WeatherEventAvailability VALUES(?,?)",
            ("WTH-FUTURE-TEST", (future + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")))
        connection.execute("""INSERT INTO WeatherForecast(forecast_id,route_id,facility_id,issued_at,ingestion_time,valid_from,valid_to,
            condition,severity,precipitation_mm,wind_speed_kph) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("FCST-FUTURE-TEST", route_id, None, future.isoformat().replace("+00:00", "Z"),
             (future + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"), future.isoformat().replace("+00:00", "Z"),
             future_end.isoformat().replace("+00:00", "Z"), "storm", 5, 80, 100))

        obs = future.isoformat().replace("+00:00", "Z")
        connection.execute("""INSERT INTO FacilityQueue(queue_id,facility_id,observed_at,queue_length,estimated_wait_minutes,capacity,source)
            VALUES(?,?,?,?,?,?,?)""", ("FQ-FUTURE-TEST", facility_id, obs, 999, 999, 1, "sensor"))
        connection.execute("INSERT INTO FacilityQueueAvailability VALUES(?,?)",
            ("FQ-FUTURE-TEST", (future + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")))

        carrier_id = connection.execute("SELECT carrier_id FROM Shipment WHERE shipment_id=?", (shipment_id,)).fetchone()[0]
        other_shipment, other_leg, other_facility = connection.execute("""SELECT s.shipment_id,l.leg_id,l.origin_facility_id
            FROM Shipment s JOIN ShipmentLeg l USING(shipment_id) WHERE s.carrier_id=? AND s.shipment_id<>? ORDER BY s.shipment_id LIMIT 1""",
            (carrier_id, shipment_id)).fetchone()
        past_event = (cutoff - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        late_ingestion = (cutoff + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        future_event = (cutoff + timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
        connection.executemany("""INSERT INTO TrackingEvent(event_id,shipment_id,leg_id,facility_id,event_type,event_time,
            ingestion_time,source) VALUES(?,?,?,?,?,?,?,?)""", [
            (f"EVT-{other_shipment}-999991", other_shipment, other_leg, other_facility, "delivered", past_event, late_ingestion, "system"),
            (f"EVT-{other_shipment}-999992", other_shipment, other_leg, other_facility, "delivered", future_event, late_ingestion, "system"),
        ])

        version_id = f"FUTURE-PLAN-{shipment_id}"
        max_version = connection.execute("SELECT MAX(version_no) FROM ShipmentRoutePlanVersion WHERE shipment_id=?", (shipment_id,)).fetchone()[0]
        connection.execute("INSERT INTO ShipmentRoutePlanVersion VALUES(?,?,?,?,?)",
            (version_id, shipment_id, max_version + 1, future.isoformat().replace("+00:00", "Z"), "route_change"))
        core_legs = connection.execute("""SELECT leg_number,route_id,origin_facility_id,destination_facility_id,
            planned_departure_time,planned_arrival_time FROM ShipmentLeg WHERE shipment_id=? ORDER BY leg_number""", (shipment_id,)).fetchall()
        for leg in core_legs:
            connection.execute("""INSERT INTO ShipmentRoutePlanLeg VALUES(?,?,?,?,?,?,?,?)""",
                (f"{version_id}-{leg[0]:02d}", version_id, leg[0], leg[1], leg[2], leg[3], leg[4], leg[5]))
        connection.commit()

    altered_output = tmp_path / "altered_features"
    build_feature_dataset(changed_db, altered_output)
    before = _csv_row(realistic_bundle / "features/features.csv", "eta_id", eta_id)
    after = _csv_row(altered_output / "features.csv", "eta_id", eta_id)
    for name, expected in before.items():
        actual = after[name]
        assert ("" if pd.isna(expected) else str(expected)) == actual, name


def test_current_shipment_is_excluded_from_carrier_history_and_actual_outcome_is_not_a_predictor(realistic_bundle, tmp_path):
    features = pd.read_csv(realistic_bundle / "features/features.csv")
    sample = features.iloc[70]
    original = _csv_row(realistic_bundle / "features/features.csv", "eta_id", sample.eta_id)
    db_path = tmp_path / "own_history.sqlite"
    _copy_database(realistic_bundle, db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        leg, facility = connection.execute("SELECT leg_id,origin_facility_id FROM ShipmentLeg WHERE shipment_id=? ORDER BY leg_number LIMIT 1", (sample.shipment_id,)).fetchone()
        cutoff = datetime.fromisoformat(str(sample.predicted_at).replace("Z", "+00:00"))
        past = (cutoff - timedelta(minutes=4)).isoformat().replace("+00:00", "Z")
        connection.execute("""INSERT INTO TrackingEvent(event_id,shipment_id,leg_id,facility_id,event_type,event_time,ingestion_time,source)
            VALUES(?,?,?,?,?,?,?,?)""", (f"EVT-{sample.shipment_id}-999993", sample.shipment_id, leg, facility, "delivered", past, past, "system"))
        connection.execute("UPDATE Shipment SET actual_delivery_time=? WHERE shipment_id=?",
            ((cutoff + timedelta(hours=3)).isoformat().replace("+00:00", "Z"), sample.shipment_id))
        connection.commit()
    altered = tmp_path / "own_features"
    build_feature_dataset(db_path, altered)
    current = _csv_row(altered / "features.csv", "eta_id", sample.eta_id)
    assert current["carrier_historical_on_time_rate"] == original["carrier_historical_on_time_rate"]
    assert current["carrier_historical_delivery_count"] == original["carrier_historical_delivery_count"]
    headers = set(pd.read_csv(realistic_bundle / "features/features.csv", nrows=0).columns)
    assert not headers & {"actual_delivery_time", "target_delivery_time", "target_remaining_hours", "ground_truth", "anomaly_type"}


def test_batch_size_independence_and_foreign_keys(tmp_path):
    config = GenerationConfig(80, 12, 20, 480, 20261022, True)
    first, second = tmp_path / "batch20", tmp_path / "batch40"
    generate_dataset(config, first, batch_size=20)
    generate_dataset(config, second, batch_size=40)
    csv_files = {path.name for path in first.glob("*.csv")}
    assert csv_files == {path.name for path in second.glob("*.csv")}
    assert all((first / name).read_bytes() == (second / name).read_bytes() for name in csv_files)
    assert (first / "features/features.csv").read_bytes() == (second / "features/features.csv").read_bytes()
    assert (first / "features/targets.csv").read_bytes() == (second / "features/targets.csv").read_bytes()
    assert validate_dataset(first / "logitrack.sqlite", first) == []
    for db in (first / "logitrack.sqlite", second / "logitrack.sqlite"):
        with sqlite3.connect(db) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_output_manifest_and_feature_validation(realistic_bundle):
    manifest = json.loads((realistic_bundle / "source_manifest.json").read_text(encoding="utf-8"))
    features = json.loads((realistic_bundle / "features/validation_summary.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == "realistic_v2"
    assert features["validation_status"] == "PASS"
    assert features["checks"]["carrier_history_excludes_current_and_requires_both_delivery_times"]
    assert features["checks"]["queue_requires_observed_and_ingestion_cutoffs"]
    assert features["checks"]["realized_weather_requires_observed_and_ingestion_cutoffs"]
    assert features["checks"]["forecast_requires_issue_and_ingestion_cutoffs"]
    assert features["checks"]["route_plan_requires_known_at_cutoff"]
    assert {"ingestion_time_confused_with_event_time", "impossible_route_order", "future_event_leakage_in_eta", "delivered_marked_in_transit"} == set(
        pd.read_csv(realistic_bundle / "anomalyaudit.csv").anomaly_type
    )
