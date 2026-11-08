import csv
import json
import sqlite3
import pytest

from src.generator.generate import GenerationConfig, generate_dataset
from src.validation.dataset import REQUIRED_ANOMALIES, validate_dataset


def test_starter_generation_and_integrity(tmp_path):
    summary = generate_dataset(GenerationConfig.profile("starter"), tmp_path)
    assert summary["row_counts"]["Shipment"] == 100
    assert summary["row_counts"]["TrackingEvent"] == 600
    assert validate_dataset(tmp_path / "logitrack.sqlite", tmp_path) == []
    with sqlite3.connect(tmp_path / "logitrack.sqlite") as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deterministic_required_ids_and_ground_truth_separation(tmp_path):
    generate_dataset(GenerationConfig.profile("starter"), tmp_path)
    with sqlite3.connect(tmp_path / "logitrack.sqlite") as connection:
        shipment_ids = [r[0] for r in connection.execute("SELECT shipment_id FROM Shipment ORDER BY shipment_id")]
        leg_ids = [r[0] for r in connection.execute("SELECT leg_id FROM ShipmentLeg LIMIT 3")]
        event_ids = [r[0] for r in connection.execute("SELECT event_id FROM TrackingEvent WHERE shipment_id='SHP-2026-000001' ORDER BY event_id")]
        eta_id = connection.execute("SELECT eta_id FROM ETAEstimate WHERE shipment_id='SHP-2026-000001'").fetchone()[0]
        delay_ids = [r[0] for r in connection.execute("SELECT delay_reason_id FROM DelayReason")]
        source_tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        shipment_columns = {r[1] for r in connection.execute("PRAGMA table_info(Shipment)")}
        anomaly_types = {r[0] for r in connection.execute("SELECT anomaly_type FROM AnomalyAudit")}
        annotation_columns = {}
        for table in ("Shipment", "ShipmentLeg", "TrackingEvent", "Carrier", "Route", "Facility",
                      "FacilityQueue", "WeatherEvent", "ServiceLevel", "ETAEstimate", "DelayReason"):
            annotation_columns[table] = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")} & {
                "anomaly_type", "anomaly_id", "anomaly_evidence_id", "on_time", "delayed", "label_rule"
            }
    assert shipment_ids[0] == "SHP-2026-000001"
    assert all(i.startswith("LEG-SHP-2026-000") for i in leg_ids)
    assert event_ids[0] == "EVT-SHP-2026-000001-000001"
    assert eta_id.startswith("ETA-SHP-2026-000001-")
    assert delay_ids and delay_ids[0] == "DLY-2026-000001"
    assert "ground_truth" not in source_tables
    assert "delayed" not in shipment_columns and "anomaly_type" not in shipment_columns
    assert REQUIRED_ANOMALIES <= anomaly_types
    assert not any(annotation_columns.values())

    with (tmp_path / "ground_truth.csv").open(newline="", encoding="utf-8") as handle:
        labels = list(csv.DictReader(handle))
    assert len(labels) == 100
    manifest = json.loads((tmp_path / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["ground_truth_separate"] is True


def test_anomaly_typed_links_and_event_time_evidence(tmp_path):
    generate_dataset(GenerationConfig.profile("starter"), tmp_path)
    with sqlite3.connect(tmp_path / "logitrack.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        timing = connection.execute("SELECT * FROM AnomalyAudit WHERE anomaly_type=?",
            ("ingestion_time_confused_with_event_time",)).fetchone()
        event = connection.execute("SELECT * FROM TrackingEvent WHERE event_id=?",
            ("EVT-SHP-2026-000001-000002",)).fetchone()
        assert timing["target_entity_type"] == "TrackingEvent"
        assert timing["target_record_id"] == event["event_id"]
        assert timing["expected_value"] == event["ingestion_time"]
        assert timing["observed_value"] == event["event_time"]
        assert event["event_time"] != event["ingestion_time"]

        route = connection.execute("SELECT * FROM AnomalyAudit WHERE anomaly_type=?",
            ("impossible_route_order",)).fetchone()
        sequence = list(connection.execute("""SELECT a.sequence_position,a.expected_position,l.leg_id,
            l.origin_facility_id,l.destination_facility_id FROM AnomalyRouteSequence a
            JOIN ShipmentLeg l USING(leg_id) WHERE a.anomaly_id=? ORDER BY a.sequence_position""",
            (route["anomaly_id"],)))
        assert route["target_entity_type"] == "ShipmentLeg"
        assert route["target_record_id"] in {r["leg_id"] for r in sequence}
        assert [r["leg_id"] for r in sequence] == json.loads(route["observed_value"])
        assert any(a["destination_facility_id"] != b["origin_facility_id"]
                   for a, b in zip(sequence, sequence[1:]))
        assert sorted(r["expected_position"] for r in sequence) == list(range(1, len(sequence) + 1))

        lifecycle = connection.execute("SELECT * FROM AnomalyAudit WHERE anomaly_type=?",
            ("delivered_marked_in_transit",)).fetchone()
        observation = connection.execute("""SELECT o.*,e.event_time,e.ingestion_time,s.lifecycle_state
            FROM AnomalyStateObservation o JOIN TrackingEvent e ON e.event_id=o.source_event_id
            JOIN Shipment s ON s.shipment_id=o.shipment_id WHERE o.anomaly_id=?""",
            (lifecycle["anomaly_id"],)).fetchone()
        assert lifecycle["target_entity_type"] == "Shipment"
        assert lifecycle["target_record_id"] == "SHP-2026-000004"
        assert observation["observed_state"] == "in_transit"
        assert observation["lifecycle_state"] == "delivered"
        assert observation["observed_at"] > observation["ingestion_time"] > observation["event_time"]


def test_eta_leakage_evidence_is_real_and_separate(tmp_path):
    generate_dataset(GenerationConfig.profile("starter"), tmp_path)
    with sqlite3.connect(tmp_path / "logitrack.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        audit = connection.execute("SELECT * FROM AnomalyAudit WHERE anomaly_type=?",
            ("future_event_leakage_in_eta",)).fetchone()
        evidence = connection.execute("""SELECT x.*,e.event_type,s.lifecycle_state FROM AnomalyETAInputEvidence x
            JOIN TrackingEvent e ON e.event_id=x.event_id JOIN Shipment s ON s.shipment_id=x.shipment_id
            WHERE x.anomaly_id=?""", (audit["anomaly_id"],)).fetchone()
        assert audit["target_entity_type"] == "ETAEstimate"
        assert audit["target_record_id"] == evidence["eta_id"]
        assert evidence["event_id"] == "EVT-SHP-2026-000003-000004"
        assert evidence["event_time"] > evidence["predicted_at"]
        assert evidence["ingestion_time"] > evidence["predicted_at"]
        assert evidence["feature_name"] == "latest_event_type"
        assert evidence["feature_value"] == evidence["event_type"]
        assert evidence["intentionally_included"] == 1
        assert evidence["lifecycle_state"] == "delivered"


def test_csv_foreign_keys_operational_separation_and_audit_validation(tmp_path):
    generate_dataset(GenerationConfig.profile("starter"), tmp_path)
    assert validate_dataset(tmp_path / "logitrack.sqlite", tmp_path) == []
    with sqlite3.connect(tmp_path / "logitrack.sqlite") as connection:
        for table in ("Shipment", "ShipmentLeg", "TrackingEvent", "Carrier", "Route", "Facility",
                      "FacilityQueue", "WeatherEvent", "ServiceLevel", "ETAEstimate", "DelayReason"):
            columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
            assert not columns & {"anomaly_type", "anomaly_id", "anomaly_evidence_id", "on_time", "delayed", "label_rule"}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_generation_is_reproducible(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    config = GenerationConfig.profile("starter")
    generate_dataset(config, first)
    generate_dataset(config, second)
    first_files = {path.name for path in first.iterdir() if path.is_file()}
    second_files = {path.name for path in second.iterdir() if path.is_file()}
    assert first_files == second_files
    assert all((first / name).read_bytes() == (second / name).read_bytes() for name in first_files)


def test_generation_is_batch_size_independent(tmp_path):
    first, second = tmp_path / "batch500", tmp_path / "batch1000"
    config = GenerationConfig.profile("starter")
    generate_dataset(config, first, batch_size=16)
    generate_dataset(config, second, batch_size=37)
    csv_names = {p.name for p in first.glob("*.csv")}
    assert csv_names == {p.name for p in second.glob("*.csv")}
    assert all((first / name).read_bytes() == (second / name).read_bytes() for name in csv_names)
    for db in (first / "logitrack.sqlite", second / "logitrack.sqlite"):
        with sqlite3.connect(db) as con:
            assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(first / "logitrack.sqlite") as a, sqlite3.connect(second / "logitrack.sqlite") as b:
        tables = [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            assert list(a.execute(f'SELECT * FROM "{table}" ORDER BY 1')) == list(b.execute(f'SELECT * FROM "{table}" ORDER BY 1'))


def test_failed_generation_does_not_publish_final_bundle(tmp_path, monkeypatch):
    from src.validation import dataset as validator
    output = tmp_path / "failed"
    monkeypatch.setattr(validator, "validate_dataset", lambda *_: ["forced validation failure"])
    with pytest.raises(ValueError, match="forced validation failure"):
        generate_dataset(GenerationConfig.profile("starter"), output)
    assert not output.exists()
    assert not list(tmp_path.glob(".failed.staging-*"))


def test_batch_size_must_accommodate_anomaly_evidence(tmp_path):
    with pytest.raises(ValueError, match="at least 4"):
        generate_dataset(GenerationConfig.profile("starter"), tmp_path / "too_small", batch_size=3)
