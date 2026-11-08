"""Integrity checks for generated LogiTrack bundles."""

import csv
import json
import sqlite3
from pathlib import Path

from src.database.schema import CORE_TABLES

REQUIRED_ANOMALIES = {
    "ingestion_time_confused_with_event_time",
    "impossible_route_order",
    "future_event_leakage_in_eta",
    "delivered_marked_in_transit",
}
ANOMALY_TARGETS = {
    "ingestion_time_confused_with_event_time": ("TrackingEvent", "event_id"),
    "impossible_route_order": ("ShipmentLeg", "leg_id"),
    "future_event_leakage_in_eta": ("ETAEstimate", "eta_id"),
    "delivered_marked_in_transit": ("Shipment", "shipment_id"),
}
ANOMALY_EVIDENCE_TABLES = (
    "AnomalyRouteSequence", "AnomalyStateObservation", "AnomalyETAInputEvidence",
)
OPERATIONAL_ANNOTATION_FIELDS = {
    "anomaly_type", "anomaly_id", "anomaly_evidence_id", "label_rule",
    "on_time", "delayed", "delay_reason_code",
}


def _delivery_label_mismatches(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Compare labels at integer-second precision to avoid julianday rounding."""
    return connection.execute("""SELECT g.shipment_id FROM gt g JOIN Shipment s USING(shipment_id)
        JOIN ServiceLevel sl USING(service_level_id)
        WHERE CAST(g.delayed AS INTEGER) !=
          (CAST(strftime('%s',s.actual_delivery_time) AS INTEGER) >
           CAST(strftime('%s',s.planned_delivery_time) AS INTEGER) + sl.on_time_tolerance_minutes*60)
        OR CAST(g.on_time AS INTEGER) != 1-CAST(g.delayed AS INTEGER)
        OR CAST(g.delay_minutes AS INTEGER) != max(0, CAST((
          CAST(strftime('%s',s.actual_delivery_time) AS INTEGER) -
          CAST(strftime('%s',s.planned_delivery_time) AS INTEGER))/60 AS INTEGER))
        LIMIT 5""").fetchall()


def validate_dataset(database_path: str | Path, output_dir: str | Path) -> list[str]:
    """Validate incrementally; CSV keys are staged on disk in temporary SQLite tables."""
    errors: list[str] = []
    root = Path(output_dir)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=FILE")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        required = set(CORE_TABLES) | {"CarrierCalendar", "FacilityCalendar", "AnomalyAudit", *ANOMALY_EVIDENCE_TABLES}
        manifest_path = root / "source_manifest.json"
        profile = ""
        if manifest_path.exists():
            try:
                profile = json.loads(manifest_path.read_text(encoding="utf-8")).get("profile", "")
            except (OSError, json.JSONDecodeError):
                errors.append("source_manifest.json is unreadable")
        if profile == "realistic_v2":
            required |= {
                "WeatherEventAvailability", "FacilityQueueAvailability", "WeatherForecast",
                "ShipmentRoutePlanVersion", "ShipmentRoutePlanLeg", "ShipmentRouteChange",
                "ShipmentLegOutcomeComponent",
            }
        if required - tables:
            return [f"missing tables: {sorted(required - tables)}"]
        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(5)
        if violations:
            errors.append(f"SQLite foreign-key violations: {[tuple(r) for r in violations]}")
        connection.execute("CREATE TEMP TABLE gt(shipment_id TEXT PRIMARY KEY, delayed INTEGER, on_time INTEGER, delay_minutes INTEGER, anomaly_type TEXT, anomaly_id TEXT, anomaly_evidence_id TEXT)")
        csv_columns: dict[str, set[str]] = {}
        for table in sorted(required):
            info = list(connection.execute(f'PRAGMA table_info("{table}")'))
            pks = [r["name"] for r in sorted((x for x in info if x["pk"]), key=lambda x: x["pk"])]
            if len(pks) != 1:
                errors.append(f"{table} must have one primary-key column; found {pks}"); continue
            pk = pks[0]
            stage = f"csvkey_{table}"
            connection.execute(f'CREATE TEMP TABLE "{stage}" (id TEXT PRIMARY KEY)')
            path = root / f"{table.lower()}.csv"
            if not path.exists():
                errors.append(f"missing CSV export for {table}: {path.name}"); continue
            row_count = 0
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                csv_columns[table] = set(reader.fieldnames or ())
                fk_specs = list(connection.execute(f'PRAGMA foreign_key_list("{table}")'))
                for row in reader:
                    row_count += 1
                    value = row.get(pk, "")
                    if not value:
                        if len(errors) < 100: errors.append(f"{table}.csv has blank {pk}")
                        continue
                    try:
                        connection.execute(f'INSERT INTO "{stage}" VALUES (?)', (value,))
                    except sqlite3.IntegrityError:
                        if len(errors) < 100: errors.append(f"{table}.csv has duplicate {pk}: {value}")
                    for fk in fk_specs:
                        child_value = row.get(fk["from"], "")
                        if child_value and not connection.execute(f'SELECT 1 FROM "{fk["table"]}" WHERE "{fk["to"]}"=?', (child_value,)).fetchone():
                            if len(errors) < 100: errors.append(f"{table}.{fk['from']} CSV foreign-key violation: {value} -> {child_value}")
            db_count = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            staged_count = int(connection.execute(f'SELECT COUNT(*) FROM "{stage}"').fetchone()[0])
            if row_count != db_count or staged_count != db_count:
                errors.append(f"{table} CSV/database row count mismatch: {row_count} vs {db_count}")
            mismatch = connection.execute(f'SELECT id FROM "{stage}" EXCEPT SELECT "{pk}" FROM "{table}" LIMIT 1').fetchone()
            reverse = connection.execute(f'SELECT "{pk}" FROM "{table}" EXCEPT SELECT id FROM "{stage}" LIMIT 1').fetchone()
            if mismatch or reverse:
                errors.append(f"{table} CSV primary keys differ from SQLite")
        gt_path = root / "ground_truth.csv"
        if not gt_path.exists():
            errors.append("missing ground_truth.csv")
        else:
            with gt_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    try:
                        connection.execute("INSERT INTO gt VALUES (?,?,?,?,?,?,?)", (row.get("shipment_id"), row.get("delayed"), row.get("on_time"), row.get("delay_minutes"), row.get("anomaly_type"), row.get("anomaly_id"), row.get("anomaly_evidence_id")))
                    except sqlite3.IntegrityError:
                        errors.append(f"ground_truth.csv duplicate shipment_id: {row.get('shipment_id')}")
        for table in CORE_TABLES:
            cols = {r["name"] for r in connection.execute(f'PRAGMA table_info("{table}")')}
            if cols & OPERATIONAL_ANNOTATION_FIELDS:
                errors.append(f"{table} contains ground-truth annotation columns")
            if csv_columns.get(table, set()) & OPERATIONAL_ANNOTATION_FIELDS:
                errors.append(f"{table}.csv contains ground-truth annotation columns")
        gt_count = int(connection.execute("SELECT COUNT(*) FROM gt").fetchone()[0])
        ship_count = int(connection.execute("SELECT COUNT(*) FROM Shipment").fetchone()[0])
        if gt_count != ship_count:
            errors.append(f"ground truth has {gt_count} shipments; expected {ship_count}")
        bad_labels = _delivery_label_mismatches(connection)
        if bad_labels:
            errors.append(f"derived label mismatches: {[r[0] for r in bad_labels]}")
        audit = {r["anomaly_type"]: r for r in connection.execute("SELECT * FROM AnomalyAudit")}
        if REQUIRED_ANOMALIES - set(audit):
            errors.append(f"missing audit anomaly types: {sorted(REQUIRED_ANOMALIES - set(audit))}")
        if len(audit) != len(REQUIRED_ANOMALIES):
            errors.append(f"expected {len(REQUIRED_ANOMALIES)} audit records, found {len(audit)}")
        for anomaly_type, (table, key) in ANOMALY_TARGETS.items():
            row = audit.get(anomaly_type)
            if not row: continue
            if row["target_entity_type"] != table or not connection.execute(f'SELECT 1 FROM "{table}" WHERE "{key}"=?', (row["target_record_id"],)).fetchone():
                errors.append(f"{anomaly_type} audit target does not resolve")
            label = connection.execute("SELECT anomaly_type,anomaly_id FROM gt WHERE shipment_id=?", (row["shipment_id"],)).fetchone()
            if not label or label["anomaly_type"] != anomaly_type or label["anomaly_id"] != row["anomaly_id"]:
                errors.append(f"{anomaly_type} audit and ground-truth labels do not match")
            evidence_id = row["target_record_id"]
            evidence_table, evidence_key = {
                "impossible_route_order": ("AnomalyRouteSequence", "sequence_record_id"),
                "future_event_leakage_in_eta": ("AnomalyETAInputEvidence", "input_snapshot_id"),
                "delivered_marked_in_transit": ("AnomalyStateObservation", "state_observation_id"),
            }.get(anomaly_type, (None, None))
            if evidence_table:
                evidence_row = connection.execute(f'SELECT "{evidence_key}" FROM "{evidence_table}" WHERE anomaly_id=? ORDER BY 1 LIMIT 1', (row["anomaly_id"],)).fetchone()
                evidence_id = evidence_row[0] if evidence_row else None
            gt_evidence = connection.execute("SELECT anomaly_evidence_id FROM gt WHERE shipment_id=?", (row["shipment_id"],)).fetchone()
            if not gt_evidence or gt_evidence[0] != evidence_id:
                errors.append(f"{anomaly_type} ground-truth evidence ID does not match its audit record")
        timing = audit.get("ingestion_time_confused_with_event_time")
        if timing:
            event = connection.execute("SELECT event_time,ingestion_time FROM TrackingEvent WHERE event_id=?", (timing["target_record_id"],)).fetchone()
            if not event or timing["expected_value"] != event["ingestion_time"] or timing["observed_value"] != event["event_time"]:
                errors.append("ingestion-time anomaly values do not derive from linked event")
        route = audit.get("impossible_route_order")
        if route:
            rows = list(connection.execute("SELECT a.sequence_position,a.expected_position,l.* FROM AnomalyRouteSequence a JOIN ShipmentLeg l USING(leg_id) WHERE a.anomaly_id=? ORDER BY a.sequence_position", (route["anomaly_id"],)))
            actual = [r[0] for r in connection.execute("SELECT leg_id FROM ShipmentLeg WHERE shipment_id=? ORDER BY leg_number", (route["shipment_id"],))]
            observed = [r["leg_id"] for r in rows]
            if (not rows or route["target_record_id"] != rows[0]["leg_id"]
                    or not any(a["destination_facility_id"] != b["origin_facility_id"] for a,b in zip(rows,rows[1:]))
                    or len(rows) != len(actual) or json.loads(route["expected_value"]) != actual
                    or json.loads(route["observed_value"]) != observed
                    or any(r["expected_position"] != r["leg_number"] for r in rows)):
                errors.append("impossible-route evidence is not a non-contiguous sequence")
        leak = audit.get("future_event_leakage_in_eta")
        if leak:
            ev = connection.execute("SELECT a.*,t.event_time AS source_event_time,t.ingestion_time AS source_ingestion,t.event_type,e.predicted_at AS eta_predicted_at FROM AnomalyETAInputEvidence a JOIN TrackingEvent t USING(event_id) JOIN ETAEstimate e USING(eta_id) WHERE a.anomaly_id=? AND a.predicted_at=e.predicted_at AND a.shipment_id=t.shipment_id", (leak["anomaly_id"],)).fetchone()
            if (not ev or ev["eta_id"] != leak["target_record_id"]
                    or ev["event_time"] != ev["source_event_time"] or ev["ingestion_time"] != ev["source_ingestion"]
                    or ev["feature_value"] != ev["event_type"] or not ev["intentionally_included"]
                    or (ev["event_time"] <= ev["predicted_at"] and ev["ingestion_time"] <= ev["predicted_at"])):
                errors.append("ETA leakage evidence does not reference an unavailable event")
        state = audit.get("delivered_marked_in_transit")
        if state and not connection.execute("SELECT 1 FROM AnomalyStateObservation o JOIN TrackingEvent e ON e.event_id=o.source_event_id JOIN Shipment s USING(shipment_id) WHERE o.anomaly_id=? AND e.event_type='delivered' AND o.observed_state='in_transit' AND s.lifecycle_state='delivered' AND o.observed_at>e.event_time AND o.observed_at>e.ingestion_time", (state["anomaly_id"],)).fetchone():
            errors.append("delivered/in-transit evidence is not a traceable post-delivery observation")
    connection.close()
    return errors


def _plus_minutes(timestamp: str, minutes: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _whole_minutes(later: str, earlier: str) -> int:
    from datetime import datetime
    return int((datetime.fromisoformat(later.replace("Z", "+00:00")) -
                datetime.fromisoformat(earlier.replace("Z", "+00:00"))).total_seconds() / 60)
