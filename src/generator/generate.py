"""Deterministic synthetic logistics dataset generator.

The source tables contain operational records. Derived delivery labels and the
four injected anomaly annotations are written to separate ground-truth/audit
outputs and are never joined into model features by this module.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import shutil
import tempfile
import hashlib
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import (
    FULL_DATASET_SIZE,
    FULL_DISRUPTION_WINDOW_COUNT,
    FULL_FACILITY_COUNT,
    FULL_TRACKING_EVENT_COUNT,
    SEED,
    STARTER_DATASET_SIZE,
)
from src.database.init_db import initialize_database

MODES = ("parcel", "ltl", "ftl", "air", "ocean", "rail", "last_mile")
EVENT_TYPES = ("label_created", "picked_up", "departed", "arrived", "out_for_delivery", "delivered")
WEATHER = ("rain", "snow", "ice", "fog", "storm", "extreme_heat", "wind")
REASONS = ("weather", "facility_congestion", "carrier_capacity", "mechanical", "customs", "missed_connection")
ANCHORS = ((37.77, -122.42), (40.71, -74.01), (41.88, -87.63), (29.76, -95.37),
           (47.61, -122.33), (33.45, -112.07), (51.51, -0.13), (52.52, 13.40),
           (19.08, 72.88), (28.61, 77.21), (35.68, 139.69), (-33.87, 151.21))
COUNTRIES = ("US", "GB", "DE", "IN", "JP", "AU")
BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class GenerationConfig:
    shipments: int = STARTER_DATASET_SIZE
    facilities: int = 12
    disruption_windows: int = 20
    tracking_events: int | None = None
    seed: int = SEED
    realistic_v2: bool = False

    def __post_init__(self) -> None:
        if self.shipments < 4:
            raise ValueError("shipments must be at least 4 to include all labelled anomaly cases")
        if self.facilities < 4:
            raise ValueError("facilities must be at least 4")
        if self.disruption_windows < 0:
            raise ValueError("disruption_windows cannot be negative")
        if self.tracking_events is not None and self.tracking_events < self.shipments * 6:
            raise ValueError("tracking_events must be at least six per shipment to retain milestone coverage")

    @classmethod
    def profile(cls, name: str, seed: int = SEED) -> "GenerationConfig":
        if name == "starter":
            return cls(STARTER_DATASET_SIZE, 12, 20, seed=seed)
        if name == "full":
            return cls(FULL_DATASET_SIZE, FULL_FACILITY_COUNT, FULL_DISRUPTION_WINDOW_COUNT,
                       FULL_TRACKING_EVENT_COUNT, seed)
        if name == "realistic_v2":
            return cls(FULL_DATASET_SIZE, FULL_FACILITY_COUNT, FULL_DISRUPTION_WINDOW_COUNT,
                       FULL_TRACKING_EVENT_COUNT, seed, True)
        raise ValueError(f"unknown profile: {name}")


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _plus(value: str, minutes: float) -> str:
    return _stamp(datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(minutes=minutes))


def _insert_rows(connection: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    connection.executemany(sql, ([row[column] for column in columns] for row in rows))


def _make_records(config: GenerationConfig, *, shipment_start: int = 1,
                  shipment_count: int | None = None, include_anomalies: bool = True,
                  include_calendars: bool = True) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {name: [] for name in (
        "Carrier", "Facility", "ServiceLevel", "Route", "Shipment", "ShipmentLeg",
        "TrackingEvent", "FacilityQueue", "WeatherEvent", "ETAEstimate", "DelayReason",
        "CarrierCalendar", "FacilityCalendar", "AnomalyAudit", "AnomalyRouteSequence",
        "AnomalyStateObservation", "AnomalyETAInputEvidence", "ground_truth")}
    for support_table in ("WeatherEventAvailability", "FacilityQueueAvailability", "WeatherForecast",
                          "ShipmentRoutePlanVersion", "ShipmentRoutePlanLeg", "ShipmentRouteChange",
                          "ShipmentLegOutcomeComponent"):
        records[support_table] = []

    # Parent/master entities first.
    carrier_types = list(MODES)
    for index in range(1, 21):
        records["Carrier"].append(dict(carrier_id=f"CAR-{index:06d}", carrier_name=f"Synthetic Carrier {index:03d}",
            carrier_type=carrier_types[(index - 1) % len(carrier_types)], home_country=COUNTRIES[(index - 1) % len(COUNTRIES)],
            active=1, effective_from="2025-01-01T00:00:00Z", effective_to=None))

    facility_type = ("warehouse", "hub", "port", "airport", "cross_dock", "customer")
    for index in range(config.facilities):
        rng = random.Random(_stable_seed(config.seed, "facility", index + 1))
        anchor_lat, anchor_lon = ANCHORS[index % len(ANCHORS)]
        lat = max(-89.0, min(89.0, anchor_lat + rng.uniform(-1.4, 1.4)))
        lon = max(-179.0, min(179.0, anchor_lon + rng.uniform(-1.4, 1.4)))
        records["Facility"].append(dict(facility_id=f"FAC-{index + 1:06d}", facility_name=f"Synthetic {facility_type[index % len(facility_type)].replace('_', ' ').title()} {index + 1:03d}",
            facility_type=facility_type[index % len(facility_type)], country_code=COUNTRIES[index % len(COUNTRIES)],
            timezone="UTC", latitude=round(lat, 6), longitude=round(lon, 6), active=1,
            effective_from="2025-01-01T00:00:00Z", effective_to=None))

    for index, mode in enumerate(MODES, start=1):
        records["ServiceLevel"].append(dict(service_level_id=f"SLV-{index:03d}", service_name=f"{mode.replace('_', ' ').title()} Standard",
            mode=mode, target_transit_hours=24.0 + index * 8, on_time_tolerance_minutes=30 if mode in ("parcel", "last_mile") else 60,
            active=1, effective_from="2025-01-01T00:00:00Z", effective_to=None))

    # Build a cyclic graph with one-, two-, and three-hop route options.
    # A second effective version on selected lanes records route changes.
    route_lookup: dict[tuple[int, int], list[dict[str, Any]]] = {}
    route_no = 0
    for origin in range(config.facilities):
        for hop in (1, 2, 3):
            rng = random.Random(_stable_seed(config.seed, "route", f"{origin}:{hop}"))
            dest = (origin + hop) % config.facilities
            distance = 90.0 + hop * 130.0 + rng.random() * 900.0
            mode = MODES[(origin + hop) % len(MODES)]
            route_no += 1
            route_id = f"RTE-{route_no:06d}"
            first = dict(route_id=route_id, origin_facility_id=f"FAC-{origin + 1:06d}", destination_facility_id=f"FAC-{dest + 1:06d}",
                mode=mode, distance_km=round(distance, 2), planned_transit_hours=round(distance / (55 + 8 * hop), 2), route_version=1,
                effective_from="2025-01-01T00:00:00Z", effective_to=None)
            records["Route"].append(first)
            route_lookup[(origin, dest)] = [first]
            if route_no % 7 == 0:
                first["effective_to"] = "2026-07-01T00:00:00Z"
                route_no += 1
                revised = dict(first, route_id=f"RTE-{route_no:06d}", route_version=2,
                    planned_transit_hours=round(first["planned_transit_hours"] * 1.08, 2),
                    effective_from="2026-07-01T00:00:00Z", effective_to=None)
                records["Route"].append(revised)
                route_lookup[(origin, dest)].append(revised)

    # Disruption windows drive delay mechanisms and are separate from anomaly truth.
    for index in range(config.disruption_windows):
        rng = random.Random(_stable_seed(config.seed, "disruption", index + 1))
        route = records["Route"][rng.randrange(len(records["Route"]))]
        valid_from = BASE_DATE + timedelta(days=rng.randrange(300), hours=rng.randrange(24))
        hours = rng.randint(3, 48)
        condition = WEATHER[index % len(WEATHER)]
        records["WeatherEvent"].append(dict(weather_event_id=f"WTH-{index + 1:06d}", facility_id=None,
            route_id=route["route_id"], observed_at=_stamp(valid_from - timedelta(hours=rng.randint(0, 12))),
            valid_from=_stamp(valid_from), valid_to=_stamp(valid_from + timedelta(hours=hours)), condition=condition,
            severity=1 + index % 5, precipitation_mm=round(rng.uniform(0, 80), 1),
            wind_speed_kph=round(rng.uniform(0, 120), 1), source="synthetic_weather"))

    # Stable per-facility and per-carrier operating calendars.
    if include_calendars:
        for carrier_idx in range(1, 21):
            for day in range(365):
                date = (BASE_DATE + timedelta(days=day)).date().isoformat()
                operating = int((day + carrier_idx) % 7 != 0)
                rng = random.Random(_stable_seed(config.seed, "carrier_calendar", f"{carrier_idx}:{day + 1}"))
                records["CarrierCalendar"].append(dict(calendar_id=f"CC-{carrier_idx:03d}-{day + 1:03d}", carrier_id=f"CAR-{carrier_idx:06d}",
                    calendar_date=date, is_operating=operating, capacity_factor=round(rng.uniform(0.65, 1.15), 3)))
        for facility_idx in range(config.facilities):
            for day in range(365):
                date = (BASE_DATE + timedelta(days=day)).date().isoformat()
                is_open = int((day + facility_idx) % 7 not in (0,))
                rng = random.Random(_stable_seed(config.seed, "facility_calendar", f"{facility_idx + 1}:{day + 1}"))
                records["FacilityCalendar"].append(dict(calendar_id=f"FC-{facility_idx + 1:03d}-{day + 1:03d}", facility_id=f"FAC-{facility_idx + 1:06d}",
                    calendar_date=date, is_open=is_open, capacity_factor=round(rng.uniform(0.7, 1.2), 3)))

    # Source shipment records, legs and event histories. Actual delivery time is
    # constructed from planned time plus mechanism-based operating delay.
    count = config.shipments if shipment_count is None else shipment_count
    for number in range(shipment_start, shipment_start + count):
        # Per-shipment random streams make values independent of chunk boundaries.
        rng = random.Random(_stable_seed(config.seed, "shipment", number))
        shipment_id = f"SHP-2026-{number:06d}"
        shipment_day = (number * 7 + rng.randrange(365)) % 300
        created = BASE_DATE + timedelta(days=shipment_day, minutes=rng.randrange(1440))
        planned_pickup = created + timedelta(hours=4)
        path_len = 1 + (number % 3)
        start = (number * 5) % config.facilities
        route_chain: list[dict[str, Any]] = []
        current = start
        for leg_i in range(path_len):
            hop = 1 + ((number + leg_i) % 3)
            dest = (current + hop) % config.facilities
            options = route_lookup[(current, dest)]
            route = options[-1] if created >= datetime(2026, 7, 1, tzinfo=timezone.utc) and len(options) > 1 else options[0]
            route_chain.append(route)
            current = dest
        carrier_id = f"CAR-{(number - 1) % 20 + 1:06d}"
        service_mode = route_chain[0]["mode"]
        service_id = f"SLV-{MODES.index(service_mode) + 1:03d}"
        transit = sum(route["planned_transit_hours"] for route in route_chain)
        planned_delivery = planned_pickup + timedelta(hours=transit)
        reason_code = REASONS[(number // 4) % len(REASONS)] if number % 4 == 0 else None
        reason_delay = {"weather": 300, "facility_congestion": 150, "carrier_capacity": 240,
                        "mechanical": 210, "customs": 420, "missed_connection": 360}.get(reason_code, 0)
        # Non-delayed cases finish slightly early/on-plan; delayed cases are
        # delayed by the operational mechanism above, never by an independent label draw.
        schedule_variance = rng.randint(-45, 25) if reason_code is None else reason_delay + rng.randint(0, 90)
        actual_delivery = planned_delivery + timedelta(minutes=schedule_variance)
        records["Shipment"].append(dict(shipment_id=shipment_id, external_reference=f"EXT-{number:08d}", carrier_id=carrier_id,
            route_id=route_chain[0]["route_id"], service_level_id=service_id,
            origin_facility_id=route_chain[0]["origin_facility_id"], destination_facility_id=route_chain[-1]["destination_facility_id"],
            created_at=_stamp(created), planned_pickup_time=_stamp(planned_pickup), planned_delivery_time=_stamp(planned_delivery),
            actual_pickup_time=_stamp(planned_pickup + timedelta(minutes=rng.randint(-10, 30))), actual_delivery_time=_stamp(actual_delivery),
            lifecycle_state="delivered", priority=("standard", "standard", "high", "low")[number % 4],
            weight_kg=round(rng.uniform(0.2, 900), 2), declared_value=round(rng.uniform(10, 20000), 2), currency_code="USD"))

        leg_start = planned_pickup
        for leg_index, route in enumerate(route_chain, start=1):
            leg_hours = route["planned_transit_hours"]
            leg_end = leg_start + timedelta(hours=leg_hours)
            leg_id = f"LEG-{shipment_id}-{leg_index:02d}"
            actual_leg_delay = schedule_variance if leg_index == len(route_chain) else 0
            records["ShipmentLeg"].append(dict(leg_id=leg_id, shipment_id=shipment_id, leg_number=leg_index,
                route_id=route["route_id"], carrier_id=carrier_id, origin_facility_id=route["origin_facility_id"],
                destination_facility_id=route["destination_facility_id"], planned_departure_time=_stamp(leg_start),
                planned_arrival_time=_stamp(leg_end), actual_departure_time=_stamp(leg_start),
                actual_arrival_time=_stamp(leg_end + timedelta(minutes=actual_leg_delay)), leg_state="arrived"))
            leg_start = leg_end

        nominal = [
            ("label_created", created, None, "system"),
            ("picked_up", planned_pickup, f"LEG-{shipment_id}-01", "carrier"),
            ("departed", planned_pickup + timedelta(minutes=10), f"LEG-{shipment_id}-01", "carrier"),
            ("arrived", planned_delivery - timedelta(hours=2), f"LEG-{shipment_id}-{path_len:02d}", "facility"),
            ("out_for_delivery", planned_delivery - timedelta(hours=1), f"LEG-{shipment_id}-{path_len:02d}", "carrier"),
            ("delivered", actual_delivery, f"LEG-{shipment_id}-{path_len:02d}", "carrier"),
        ]
        # Duplicate-like scan with a missing arrival (an event gap) while preserving unique IDs.
        if number % 13 == 0:
            nominal[3] = ("departed", nominal[2][1], nominal[2][2], nominal[2][3])
        # Event budget defaults to six rows per shipment. A requested budget is
        # met by adding/removing neutral in-facility heartbeat records below.
        for event_index, (event_type, event_time, leg_id, source) in enumerate(nominal, start=1):
            ingestion_lag = 48 * 60 if number % 11 == 0 and event_index == 3 else (rng.randint(2, 180) if number % 7 == 0 else rng.randint(0, 25))
            event_row = dict(event_id=f"EVT-{shipment_id}-{event_index:06d}", shipment_id=shipment_id,
                leg_id=leg_id, facility_id=(route_chain[-1]["destination_facility_id"] if event_type in ("arrived", "out_for_delivery", "delivered") else route_chain[0]["origin_facility_id"]),
                event_type=event_type, event_time=_stamp(event_time), ingestion_time=_stamp(event_time + timedelta(minutes=ingestion_lag)),
                source=source, status_detail=("synthetic delayed duplicate scan" if number % 13 == 0 and event_index == 4 else None),
                latitude=None, longitude=None)
            records["TrackingEvent"].append(event_row)

        extra_total = max(0, (config.tracking_events or config.shipments * 6) - config.shipments * 6)
        if extra_total:
            base_count = config.shipments * 6
            for cycle in range(extra_total // base_count + 1):
                for milestone in range(6):
                    extra_index = cycle * base_count + (number - 1) * 6 + milestone
                    if extra_index >= extra_total:
                        break
                    source = next(r for r in records["TrackingEvent"] if r["event_id"] == f"EVT-{shipment_id}-{milestone + 1:06d}")
                    sequence = 7 + cycle * 6 + milestone
                    clone = dict(source, event_id=f"EVT-{shipment_id}-{sequence:06d}",
                                 event_time=_plus(source["event_time"], sequence),
                                 ingestion_time=_plus(source["ingestion_time"], sequence),
                                 status_detail="synthetic heartbeat scan")
                    records["TrackingEvent"].append(clone)

        # Queue observations are generated at key facilities/times and remain
        # ordinary operational data, without label fields.
        for q in range(2):
            fac = route_chain[q % len(route_chain)]["origin_facility_id"]
            # The schema keys queue observations by facility/time/source. Shipment-keyed
            # microseconds preserve chronology while separating same-second scans.
            observed = planned_pickup + timedelta(hours=q * 6, microseconds=number * 2 + q)
            observed_at = observed.isoformat(timespec="microseconds").replace("+00:00", "Z")
            queue_len = 5 + ((number * 17 + q * 29) % 95)
            records["FacilityQueue"].append(dict(queue_id=f"FQ-{number:06d}-{q + 1:02d}", facility_id=fac,
                observed_at=observed_at, queue_length=queue_len, estimated_wait_minutes=round(queue_len * 2.4, 1),
                capacity=100, source="sensor"))

        tolerance = next(row["on_time_tolerance_minutes"] for row in records["ServiceLevel"] if row["service_level_id"] == service_id)
        delay_minutes = max(0, int((actual_delivery - planned_delivery).total_seconds() / 60))
        is_delayed = delay_minutes > tolerance
        records["ground_truth"].append(dict(shipment_id=shipment_id, actual_delivery_time=_stamp(actual_delivery),
            delay_minutes=delay_minutes, on_time=int(not is_delayed), delayed=int(is_delayed),
            delay_reason_code=reason_code if is_delayed else "none", label_rule="actual_delivery_time > planned_delivery_time + service_tolerance"))
        if is_delayed:
            event_id = f"EVT-{shipment_id}-000006"
            records["DelayReason"].append(dict(delay_reason_id=f"DLY-2026-{len(records['DelayReason']) + 1:06d}", shipment_id=shipment_id,
                leg_id=f"LEG-{shipment_id}-{path_len:02d}", event_id=event_id, reason_code=reason_code,
                reason_detail=f"Synthetic {reason_code.replace('_', ' ')} disruption", delay_minutes=delay_minutes,
                identified_at=_stamp(actual_delivery + timedelta(minutes=5)), source="carrier", is_confirmed=1))

        # Snapshot prediction is set before delivery, using planned schedule and
        # small deterministic uncertainty; actual outcome is not copied into it.
        predicted_at = planned_pickup + timedelta(hours=min(2, transit / 4))
        predicted_delivery = planned_delivery + timedelta(minutes=((number % 9) - 4) * 15)
        records["ETAEstimate"].append(dict(eta_id=f"ETA-{shipment_id}-{predicted_at.strftime('%Y%m%dT%H%M%SZ')}",
            shipment_id=shipment_id, predicted_at=_stamp(predicted_at), estimated_delivery_time=_stamp(predicted_delivery),
            prediction_horizon_hours=round(max(0, (predicted_delivery - predicted_at).total_seconds() / 3600), 2),
            model_version="baseline-v1", prediction_type="baseline", confidence=round(0.55 + (number % 40) / 100, 2),
            actual_delivery_time_at_prediction=None))

    # Keep exactly the requested event total. The default is six per shipment;
    # event count overrides are deterministic and use only valid duplicate/gap rows.
    target_events = config.tracking_events if config.tracking_events is not None else count * 6
    event_rows = records["TrackingEvent"]
    if target_events > len(event_rows) and shipment_start == 1 and count == config.shipments:
        while len(event_rows) < target_events:
            source = event_rows[(len(event_rows) - config.shipments * 6) % (config.shipments * 6)]
            clone = dict(source)
            extra_index = len(event_rows) - config.shipments * 6
            base_count = config.shipments * 6
            sequence = 7 + (extra_index // base_count) * 6 + (extra_index % 6)
            clone["event_id"] = f"EVT-{clone['shipment_id']}-{sequence:06d}"
            clone["event_time"] = _plus(source["event_time"], sequence)
            clone["ingestion_time"] = _plus(source["ingestion_time"], sequence)
            clone["status_detail"] = "synthetic heartbeat scan"
            event_rows.append(clone)
    elif target_events < len(event_rows) and shipment_start == 1 and count == config.shipments:
        event_rows[:] = event_rows[:target_events]

    if config.realistic_v2:
        from src.generator.realistic_v2 import apply_realistic_outcomes
        apply_realistic_outcomes(config, records, shipment_start)

    if not include_anomalies:
        for row in records["ground_truth"]:
            row.update(anomaly_type="none", anomaly_id=None, anomaly_evidence_id=None)
        return records

    # Required anomaly cases are represented in isolated audit evidence tables.
    # Operational source events, legs, shipment state, and ETAs remain untouched.
    if include_anomalies and shipment_start <= 1 and shipment_start + count > 4:
      anomaly_ids = {number: f"ANM-2026-{number:06d}" for number in range(1, 5)}
    anomaly_evidence: dict[str, tuple[str, str]] = {}

    timing_shipment = "SHP-2026-000001"
    timing_event_id = "EVT-SHP-2026-000001-000002"
    timing_event = next(r for r in records["TrackingEvent"] if r["event_id"] == timing_event_id)
    timing_anomaly = anomaly_ids[1]
    records["AnomalyAudit"].append(dict(anomaly_id=timing_anomaly, shipment_id=timing_shipment,
        target_entity_type="TrackingEvent", target_record_id=timing_event_id,
        anomaly_type="ingestion_time_confused_with_event_time", affected_field="ingestion_time",
        expected_value=timing_event["ingestion_time"], observed_value=timing_event["event_time"],
        detected_at="2026-12-01T00:00:00Z", severity="high"))
    anomaly_evidence[timing_shipment] = (timing_anomaly, timing_event_id)

    route_shipment = "SHP-2026-000002"
    route_legs = sorted((r for r in records["ShipmentLeg"] if r["shipment_id"] == route_shipment), key=lambda r: r["leg_number"])
    actual_leg_ids = [r["leg_id"] for r in route_legs]
    observed_leg_ids = list(reversed(actual_leg_ids))
    route_anomaly = anomaly_ids[2]
    records["AnomalyAudit"].append(dict(anomaly_id=route_anomaly, shipment_id=route_shipment,
        target_entity_type="ShipmentLeg", target_record_id=observed_leg_ids[0],
        anomaly_type="impossible_route_order", affected_field="leg_sequence",
        expected_value=json.dumps(actual_leg_ids), observed_value=json.dumps(observed_leg_ids),
        detected_at="2026-12-01T00:00:00Z", severity="critical"))
    for position, leg_id in enumerate(observed_leg_ids, start=1):
        expected_position = actual_leg_ids.index(leg_id) + 1
        records["AnomalyRouteSequence"].append(dict(
            sequence_record_id=f"RSEQ-{route_anomaly}-{position:02d}", anomaly_id=route_anomaly,
            shipment_id=route_shipment, sequence_position=position, leg_id=leg_id,
            expected_position=expected_position))
    anomaly_evidence[route_shipment] = (route_anomaly, f"RSEQ-{route_anomaly}-01")

    eta_shipment = "SHP-2026-000003"
    eta = next(r for r in records["ETAEstimate"] if r["shipment_id"] == eta_shipment)
    future_event = next(r for r in records["TrackingEvent"] if r["shipment_id"] == eta_shipment
                        and (r["event_time"] > eta["predicted_at"] or r["ingestion_time"] > eta["predicted_at"]))
    leakage_anomaly = anomaly_ids[3]
    leakage_snapshot_id = f"LEAK-{leakage_anomaly}"
    records["AnomalyAudit"].append(dict(anomaly_id=leakage_anomaly, shipment_id=eta_shipment,
        target_entity_type="ETAEstimate", target_record_id=eta["eta_id"],
        anomaly_type="future_event_leakage_in_eta", affected_field="feature_event_cutoff",
        expected_value=f"event {future_event['event_id']} excluded because ingestion_time > predicted_at",
        observed_value=f"feature {future_event['event_type']} included from {future_event['event_id']}",
        detected_at="2026-12-01T00:00:00Z", severity="high"))
    records["AnomalyETAInputEvidence"].append(dict(
        input_snapshot_id=leakage_snapshot_id, anomaly_id=leakage_anomaly,
        shipment_id=eta_shipment, eta_id=eta["eta_id"], event_id=future_event["event_id"],
        predicted_at=eta["predicted_at"], event_time=future_event["event_time"],
        ingestion_time=future_event["ingestion_time"], feature_name="latest_event_type",
        feature_value=future_event["event_type"], intentionally_included=1))
    anomaly_evidence[eta_shipment] = (leakage_anomaly, leakage_snapshot_id)

    state_shipment = "SHP-2026-000004"
    state_event = next(r for r in records["TrackingEvent"] if r["shipment_id"] == state_shipment and r["event_type"] == "delivered")
    state_anomaly = anomaly_ids[4]
    state_observation_id = f"OBS-{state_anomaly}"
    observed_at = _plus(state_event["ingestion_time"], 1)
    records["AnomalyAudit"].append(dict(anomaly_id=state_anomaly, shipment_id=state_shipment,
        target_entity_type="Shipment", target_record_id=state_shipment,
        anomaly_type="delivered_marked_in_transit", affected_field="lifecycle_state",
        expected_value="delivered", observed_value="in_transit",
        detected_at=observed_at, severity="critical"))
    records["AnomalyStateObservation"].append(dict(
        state_observation_id=state_observation_id, anomaly_id=state_anomaly,
        shipment_id=state_shipment, source_event_id=state_event["event_id"],
        observed_at=observed_at, observed_state="in_transit"))
    anomaly_evidence[state_shipment] = (state_anomaly, state_observation_id)

    # Ground truth is an independent export with one anomaly annotation per
    # intentionally labelled example; it is never merged into operational tables.
    anomaly_by_shipment = {row["shipment_id"]: row["anomaly_type"] for row in records["AnomalyAudit"]}
    audit_id_by_shipment = {row["shipment_id"]: row["anomaly_id"] for row in records["AnomalyAudit"]}
    for row in records["ground_truth"]:
        row["anomaly_type"] = anomaly_by_shipment.get(row["shipment_id"], "none")
        audit_info = anomaly_evidence.get(row["shipment_id"])
        row["anomaly_id"] = audit_id_by_shipment.get(row["shipment_id"])
        row["anomaly_evidence_id"] = audit_info[1] if audit_info else None

    return records


def _stable_seed(seed: int, namespace: str, key: int | str) -> int:
    digest = hashlib.blake2b(f"{seed}:{namespace}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def generate_dataset(config: GenerationConfig, output_dir: str | Path, batch_size: int = 500) -> dict[str, Any]:
    """Generate incrementally into a staging directory and publish only after validation.

    A failed run is cleaned up and may be restarted from scratch; resume is not supported.
    The final directory must not already exist.
    """
    if batch_size < 4:
        raise ValueError("batch_size must be at least 4 to keep all anomaly evidence in the first batch")
    output = Path(output_dir).resolve()
    if output.name.lower() == "starter" and "data" in {p.lower() for p in output.parts}:
        raise ValueError("refusing to write over the validated starter dataset")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise FileExistsError(f"output already exists and is not empty: {output}")
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    published = False
    table_order = ("Carrier", "Facility", "ServiceLevel", "Route", "Shipment", "ShipmentLeg",
                   "TrackingEvent", "FacilityQueue", "WeatherEvent", "ETAEstimate", "DelayReason",
                   "CarrierCalendar", "FacilityCalendar", "AnomalyAudit", "AnomalyRouteSequence",
                   "AnomalyStateObservation", "AnomalyETAInputEvidence")
    support_order = ("WeatherEventAvailability", "FacilityQueueAvailability", "WeatherForecast",
                     "ShipmentRoutePlanVersion", "ShipmentRoutePlanLeg", "ShipmentRouteChange",
                     "ShipmentLegOutcomeComponent")
    if config.realistic_v2:
        table_order += support_order
    counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    handles: dict[str, Any] = {}
    writers: dict[str, csv.DictWriter] = {}
    try:
        db_path = staging / "logitrack.sqlite"
        initialize_database(db_path)
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            # CSV schemas come from SQLite; ground truth is deliberately separate.
            for table in table_order:
                columns = [r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')]
                path = staging / f"{table.lower()}.csv"
                handle = path.open("w", newline="", encoding="utf-8")
                handles[table] = handle
                writers[table] = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                writers[table].writeheader()
            gt_columns = ["shipment_id", "actual_delivery_time", "delay_minutes", "on_time", "delayed",
                          "delay_reason_code", "label_rule", "anomaly_type", "anomaly_id", "anomaly_evidence_id"]
            handle = (staging / "ground_truth.csv").open("w", newline="", encoding="utf-8")
            handles["ground_truth"] = handle
            writers["ground_truth"] = csv.DictWriter(handle, fieldnames=gt_columns)
            writers["ground_truth"].writeheader()
            delay_id = 0
            for start in range(1, config.shipments + 1, batch_size):
                size = min(batch_size, config.shipments - start + 1)
                chunk = _make_records(config, shipment_start=start, shipment_count=size,
                                      include_anomalies=(start == 1 and size >= 4), include_calendars=(start == 1))
                # Master/support tables are emitted once; shipment-dependent rows are emitted by chunk.
                tables_this_chunk = set(table_order)
                if start != 1:
                    tables_this_chunk -= {"Carrier", "Facility", "ServiceLevel", "Route", "WeatherEvent",
                                          "CarrierCalendar", "FacilityCalendar", "WeatherEventAvailability",
                                          "WeatherForecast"}
                connection.execute("BEGIN")
                for table in table_order:
                    rows = chunk[table]
                    if table not in tables_this_chunk or not rows:
                        continue
                    if table == "DelayReason":
                        for row in rows:
                            delay_id += 1
                            row["delay_reason_id"] = f"DLY-2026-{delay_id:06d}"
                    _insert_rows(connection, table, rows)
                    writers[table].writerows(rows)
                    counts[table] += len(rows)
                _insert_rows(connection, "Shipment", [])  # documents parent-first transaction boundary
                gt_rows = chunk["ground_truth"]
                writers["ground_truth"].writerows(gt_rows)
                counts["ground_truth"] += len(gt_rows)
                label_counts.update(str(row["delayed"]) for row in gt_rows)
                connection.commit()
            violations = connection.execute("PRAGMA foreign_key_check").fetchmany(5)
            if violations:
                raise sqlite3.IntegrityError(f"foreign-key violations: {violations}")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for handle in handles.values():
            handle.close()
        counts["events"] = counts["TrackingEvent"]
        summary = {"profile": "realistic_v2" if config.realistic_v2 else "legacy", "seed": config.seed, "parameters": asdict(config), "batch_size": batch_size,
                   "row_counts": dict(counts), "derived_label_counts": dict(label_counts),
                   "anomaly_counts": {r["anomaly_type"]: 1 for r in _make_records(
                       GenerationConfig(4, config.facilities, 0, seed=config.seed), shipment_count=4
                   )["AnomalyAudit"]}}
        summary["outputs"] = [f"{key.lower()}.csv" for key in table_order] + ["ground_truth.csv", "logitrack.sqlite", "generation_summary.json", "source_manifest.json"]
        if config.realistic_v2:
            summary["outputs"].extend(["features/features.csv", "features/targets.csv", "features/feature_dictionary.csv", "features/validation_summary.json"])
        (staging / "generation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        manifest = {"project": "LogiTrack AI", "generator": "src.generator.generate", "profile": "realistic_v2" if config.realistic_v2 else "legacy", "seed": config.seed,
            "parameters": asdict(config), "schema_source": "src.database.schema.SCHEMA_SQL", "files": summary["outputs"],
            "row_counts": summary["row_counts"], "ground_truth_separate": True,
            "feature_leakage_policy": "Ground truth, anomaly audit, and anomaly evidence tables are not model-input tables."}
        (staging / "source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        from src.validation.dataset import validate_dataset
        errors = validate_dataset(db_path, staging)
        if errors:
            raise ValueError(f"generated bundle validation failed: {errors[:10]}")
        if config.realistic_v2:
            from src.features.build_features import build_feature_dataset
            build_feature_dataset(db_path, staging / "features")
            errors = validate_dataset(db_path, staging)
            if errors:
                raise ValueError(f"completed realistic_v2 bundle validation failed: {errors[:10]}")
        connection.close()
        if config.realistic_v2:
            output.mkdir(parents=True, exist_ok=False)
            published = True
            for path in staging.iterdir():
                if path.name == "source_manifest.json":
                    continue
                destination = output / path.name
                if path.is_dir():
                    shutil.copytree(path, destination)
                else:
                    shutil.copy2(path, destination)
            marker_tmp = output / ".source_manifest.json.tmp"
            shutil.copy2(staging / "source_manifest.json", marker_tmp)
            marker_tmp.replace(output / "source_manifest.json")
            # Publication is complete once the manifest marker is in place;
            # a Windows SQLite handle may delay removal of the private staging copy.
            shutil.rmtree(staging, ignore_errors=True)
            return summary
        output.mkdir(parents=True, exist_ok=False)
        published = True
        # Publish data files first. The manifest, written last, marks a complete bundle.
        publish_files = [p for p in staging.iterdir() if p.name not in {"generation_summary.json", "source_manifest.json"}]
        for path in publish_files:
            shutil.copy2(path, output / path.name)
        shutil.copy2(staging / "generation_summary.json", output / "generation_summary.json")
        marker_tmp = output / ".source_manifest.json.tmp"
        shutil.copy2(staging / "source_manifest.json", marker_tmp)
        marker_tmp.replace(output / "source_manifest.json")
        shutil.rmtree(staging)
        return summary
    except Exception:
        try:
            connection.close()
        except (NameError, sqlite3.ProgrammingError):
            pass
        for handle in handles.values():
            if not handle.closed:
                handle.close()
        shutil.rmtree(staging, ignore_errors=True)
        if published:
            shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("starter", "full", "realistic_v2"), default="starter")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--shipments", type=int)
    parser.add_argument("--facilities", type=int)
    parser.add_argument("--disruptions", type=int)
    parser.add_argument("--tracking-events", type=int)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--output-dir", type=Path, default=Path("data/starter"))
    args = parser.parse_args()
    base = GenerationConfig.profile(args.profile, args.seed)
    config = GenerationConfig(args.shipments or base.shipments, args.facilities or base.facilities,
        args.disruptions if args.disruptions is not None else base.disruption_windows,
        args.tracking_events if args.tracking_events is not None else base.tracking_events, args.seed, base.realistic_v2)
    print(json.dumps(generate_dataset(config, args.output_dir, batch_size=args.batch_size), indent=2))


if __name__ == "__main__":
    main()
