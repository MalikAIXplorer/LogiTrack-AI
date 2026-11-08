"""Point-in-time operational predictors for ETA snapshots.

This module intentionally does not query actual outcome columns, ground-truth
files, AnomalyAudit, or anomaly evidence. Outcome access is isolated in
``build_target_rows`` below.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

IDENTIFIER_COLUMNS = ("shipment_id", "eta_id", "predicted_at")

# Explicit allow-list: identifiers and these predictors form the feature file.
FEATURE_SPECS: dict[str, dict[str, str]] = {
    "service_mode": {"data_type": "category", "source": "effective ServiceLevel.mode", "missing": "null plus service_reference_missing flag"},
    "service_target_transit_hours": {"data_type": "float", "source": "effective ServiceLevel.target_transit_hours", "missing": "null plus service_reference_missing flag"},
    "service_reference_missing": {"data_type": "boolean", "source": "ServiceLevel effective dates", "missing": "always 0 or 1"},
    "carrier_type": {"data_type": "category", "source": "effective Carrier.carrier_type", "missing": "null plus carrier_reference_missing flag"},
    "carrier_reference_missing": {"data_type": "boolean", "source": "Carrier effective dates", "missing": "always 0 or 1"},
    "carrier_historical_on_time_rate": {"data_type": "float", "source": "Prior delivered events for this carrier with event_time and ingestion_time <= predicted_at; Beta-smoothed", "missing": "prior rate used and carrier_history_missing flag set when no history"},
    "carrier_historical_delivery_count": {"data_type": "integer", "source": "Prior delivered events available by predicted_at, excluding current shipment", "missing": "0 when no history"},
    "carrier_history_missing": {"data_type": "boolean", "source": "eligible prior delivered event availability", "missing": "always 0 or 1"},
    "shipment_priority": {"data_type": "category", "source": "Shipment.priority", "missing": "not-null reference"},
    "shipment_weight_kg": {"data_type": "float", "source": "Shipment.weight_kg", "missing": "null plus weight_missing flag"},
    "weight_missing": {"data_type": "boolean", "source": "Shipment.weight_kg", "missing": "always 0 or 1"},
    "journey_elapsed_minutes": {"data_type": "float", "source": "predicted_at - Shipment.created_at", "missing": "not-null timestamps"},
    "planned_route_distance_km": {"data_type": "float", "source": "ShipmentLeg + Route.distance_km", "missing": "0 if no remaining route legs"},
    "planned_route_hours": {"data_type": "float", "source": "ShipmentLeg + Route.planned_transit_hours", "missing": "0 if no remaining route legs"},
    "planned_leg_count": {"data_type": "integer", "source": "ShipmentLeg.leg_number", "missing": "not-null legs"},
    "remaining_scheduled_leg_count": {"data_type": "integer", "source": "ShipmentLeg.planned_arrival_time", "missing": "0 if no scheduled legs remain"},
    "remaining_scheduled_hours": {"data_type": "float", "source": "ShipmentLeg planned times", "missing": "0 if no scheduled legs remain"},
    "remaining_scheduled_distance_km": {"data_type": "float", "source": "ShipmentLeg + Route.distance_km", "missing": "0 if no scheduled legs remain"},
    "eligible_event_count": {"data_type": "integer", "source": "TrackingEvent with both times <= predicted_at", "missing": "0 when no eligible events"},
    "latest_event_type": {"data_type": "category", "source": "latest eligible TrackingEvent.event_type", "missing": "null plus latest_event_missing flag"},
    "latest_event_missing": {"data_type": "boolean", "source": "eligible TrackingEvent availability", "missing": "always 0 or 1"},
    "last_observed_state": {"data_type": "category", "source": "state inferred from latest eligible TrackingEvent", "missing": "null plus latest_event_missing flag"},
    "latest_event_age_minutes": {"data_type": "float", "source": "predicted_at - latest eligible event_time", "missing": "null plus latest_event_missing flag"},
    "latest_event_leg_number": {"data_type": "integer", "source": "latest eligible event leg_id -> ShipmentLeg.leg_number", "missing": "null if event has no leg or event is absent"},
    "pickup_scan_seen": {"data_type": "boolean", "source": "eligible TrackingEvent.event_type=picked_up", "missing": "always 0 or 1"},
    "minutes_since_pickup_scan": {"data_type": "float", "source": "predicted_at - latest eligible pickup event_time", "missing": "null plus pickup_scan_seen flag"},
    "weather_observation_count": {"data_type": "integer", "source": "route/facility WeatherEvent observed by predicted_at", "missing": "0 if none observed"},
    "active_weather_disruption_count": {"data_type": "integer", "source": "known WeatherEvent valid at predicted_at", "missing": "0 if no active disruption"},
    "max_active_weather_severity": {"data_type": "integer", "source": "active WeatherEvent.severity", "missing": "0 if none active"},
    "forecast_weather_count": {"data_type": "integer", "source": "WeatherForecast issued_at and ingestion_time <= predicted_at, valid interval overlaps route plan", "missing": "0 when no eligible forecast"},
    "max_forecast_weather_severity": {"data_type": "integer", "source": "eligible route/facility WeatherForecast.severity", "missing": "0 when no eligible forecast"},
    "forecast_precipitation_mm": {"data_type": "float", "source": "eligible WeatherForecast.precipitation_mm", "missing": "null plus forecast_weather_missing flag"},
    "forecast_wind_speed_kph": {"data_type": "float", "source": "eligible WeatherForecast.wind_speed_kph", "missing": "null plus forecast_weather_missing flag"},
    "forecast_weather_missing": {"data_type": "boolean", "source": "eligible WeatherForecast availability", "missing": "always 0 or 1"},
    "weather_observation_missing": {"data_type": "boolean", "source": "weather observation availability", "missing": "always 0 or 1"},
    "queue_observation_count": {"data_type": "integer", "source": "FacilityQueue observed by predicted_at at shipment route facilities", "missing": "0 if none observed"},
    "max_observed_queue_length": {"data_type": "integer", "source": "latest eligible FacilityQueue per facility", "missing": "null plus queue_observation_missing flag"},
    "mean_observed_wait_minutes": {"data_type": "float", "source": "latest eligible FacilityQueue per facility", "missing": "null plus queue_wait_missing flag"},
    "queue_wait_missing": {"data_type": "boolean", "source": "FacilityQueue.estimated_wait_minutes", "missing": "always 0 or 1"},
    "queue_observation_missing": {"data_type": "boolean", "source": "eligible FacilityQueue availability", "missing": "always 0 or 1"},
}

TARGET_SPECS: dict[str, dict[str, str]] = {
    "target_delivery_time": {"data_type": "datetime_utc", "source": "Shipment.actual_delivery_time", "missing": "null when shipment has no actual delivery"},
    "target_remaining_hours": {"data_type": "float", "source": "actual_delivery_time - predicted_at", "missing": "null when target is absent or not after prediction"},
    "target_eligible": {"data_type": "boolean", "source": "actual_delivery_time > predicted_at", "missing": "always 0 or 1"},
}

ALLOWED_SOURCE_TABLES = frozenset({
    "ETAEstimate", "Shipment", "ShipmentLeg", "Route", "ServiceLevel", "Carrier",
    "TrackingEvent", "FacilityQueue", "WeatherEvent", "FacilityQueueAvailability",
    "WeatherEventAvailability", "WeatherForecast", "ShipmentRoutePlanVersion", "ShipmentRoutePlanLeg",
})
FORBIDDEN_SOURCE_TABLES = frozenset({
    "ground_truth", "AnomalyAudit", "AnomalyRouteSequence", "AnomalyStateObservation",
    "AnomalyETAInputEvidence", "DelayReason", "ShipmentLegOutcomeComponent", "ShipmentRouteChange",
})

_SNAPSHOT_SQL = """
SELECT e.eta_id, e.shipment_id, e.predicted_at,
       s.created_at, s.planned_pickup_time, s.planned_delivery_time,
       s.priority, s.weight_kg, s.carrier_id,
       sl.mode AS service_mode, sl.target_transit_hours AS service_target_transit_hours,
       c.carrier_type
FROM ETAEstimate AS e
JOIN Shipment AS s ON s.shipment_id=e.shipment_id
LEFT JOIN ServiceLevel AS sl ON sl.service_level_id=s.service_level_id
 AND sl.effective_from<=e.predicted_at AND (sl.effective_to IS NULL OR sl.effective_to>e.predicted_at)
LEFT JOIN Carrier AS c ON c.carrier_id=s.carrier_id
 AND c.effective_from<=e.predicted_at AND (c.effective_to IS NULL OR c.effective_to>e.predicted_at)
ORDER BY e.predicted_at, e.eta_id
"""

_LEG_SQL = """
SELECT l.leg_id, l.leg_number, l.route_id, l.origin_facility_id,
       l.destination_facility_id, l.planned_departure_time, l.planned_arrival_time,
       r.distance_km, r.planned_transit_hours
FROM ShipmentLeg AS l JOIN Route AS r ON r.route_id=l.route_id
WHERE l.shipment_id=? ORDER BY l.leg_number
"""

_EVENT_SQL = """
SELECT event_id, leg_id, event_type, event_time, ingestion_time
FROM TrackingEvent
WHERE shipment_id=? AND event_time<=? AND ingestion_time<=?
ORDER BY event_time, ingestion_time, event_id
"""

_HISTORY_SQL = """
SELECT s.shipment_id, s.carrier_id, t.event_time, t.ingestion_time,
       CASE WHEN julianday(t.event_time) <= julianday(s.planned_delivery_time) + sl.on_time_tolerance_minutes/1440.0
            THEN 1 ELSE 0 END AS on_time
FROM TrackingEvent AS t
JOIN Shipment AS s ON s.shipment_id=t.shipment_id
JOIN ServiceLevel AS sl ON sl.service_level_id=s.service_level_id
WHERE t.event_type='delivered'
ORDER BY t.ingestion_time, t.event_time, t.event_id
"""


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _minutes(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 60.0


def _state_from_event(event_type: str | None) -> str | None:
    return {
        "label_created": "created",
        "picked_up": "in_transit",
        "departed": "in_transit",
        "arrived": "in_transit",
        "in_facility": "in_transit",
        "delay": "in_transit",
        "customs_clearance": "in_transit",
        "out_for_delivery": "out_for_delivery",
        "delivered": "delivered",
        "exception": "exception",
        "returned": "returned",
    }.get(event_type)


def iter_feature_rows(connection: sqlite3.Connection):
    """Build one metadata-plus-predictor row per ETA snapshot.

    No actual outcome, ground-truth, audit, delay-reason, or anomaly evidence
    table is queried here. TrackingEvent inclusion uses both temporal cutoffs.
    """
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=FILE")
    has_queue_availability = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='FacilityQueueAvailability'").fetchone() is not None
    queue_availability_join = "LEFT JOIN FacilityQueueAvailability AS qa USING(queue_id)" if has_queue_availability else ""
    queue_ingestion = "COALESCE(qa.ingestion_time,q.observed_at)" if has_queue_availability else "q.observed_at"
    connection.execute("DROP TABLE IF EXISTS temp.feature_queue_point")
    connection.execute(f"""CREATE TEMP TABLE feature_queue_point AS
        SELECT q.queue_id, q.facility_id, julianday(q.observed_at) AS observed_jd,
               julianday({queue_ingestion}) AS ingestion_jd, q.queue_length, q.estimated_wait_minutes
        FROM FacilityQueue AS q {queue_availability_join}""")
    connection.execute("CREATE INDEX temp.idx_feature_queue_point ON feature_queue_point(facility_id,observed_jd DESC,queue_id)")
    has_plan_history = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ShipmentRoutePlanVersion'").fetchone() is not None
    history_cursor = iter(connection.execute(_HISTORY_SQL))
    next_history = next(history_cursor, None)
    history_counts: dict[str, list[int]] = {}
    history_shipments: dict[str, tuple[str, int]] = {}
    pending_history: list[sqlite3.Row] = []

    def advance_history(cutoff: datetime) -> None:
        nonlocal next_history, pending_history
        candidates = pending_history
        pending_history = []
        while next_history is not None and _parse(next_history["ingestion_time"]) <= cutoff:
            candidates.append(next_history)
            next_history = next(history_cursor, None)
        for item in candidates:
            if _parse(item["event_time"]) > cutoff:
                pending_history.append(item)
                continue
            if item["shipment_id"] in history_shipments:
                continue
            counts = history_counts.setdefault(item["carrier_id"], [0, 0])
            is_on_time = int(item["on_time"])
            counts[0] += 1
            counts[1] += is_on_time
            history_shipments[item["shipment_id"]] = (item["carrier_id"], is_on_time)

    for snapshot in connection.execute(_SNAPSHOT_SQL):
        cutoff = _parse(snapshot["predicted_at"])
        assert cutoff is not None
        advance_history(cutoff)
        plan = None
        if has_plan_history:
            plan = connection.execute("""SELECT plan_version_id FROM ShipmentRoutePlanVersion
                WHERE shipment_id=? AND known_at<=? ORDER BY version_no DESC LIMIT 1""",
                (snapshot["shipment_id"], snapshot["predicted_at"])).fetchone()
        if plan:
            legs = list(connection.execute("""SELECT core.leg_id,p.leg_number,p.route_id,p.origin_facility_id,
                p.destination_facility_id,p.planned_departure_time,p.planned_arrival_time,r.distance_km,r.planned_transit_hours
                FROM ShipmentRoutePlanLeg AS p JOIN Route AS r USING(route_id)
                JOIN ShipmentLeg AS core ON core.shipment_id=? AND core.leg_number=p.leg_number
                WHERE p.plan_version_id=? ORDER BY p.leg_number""",
                (snapshot["shipment_id"], plan["plan_version_id"])))
        else:
            legs = list(connection.execute(_LEG_SQL, (snapshot["shipment_id"],)))
        events = list(connection.execute(_EVENT_SQL, (
            snapshot["shipment_id"], snapshot["predicted_at"], snapshot["predicted_at"],
        )))
        leg_number = {row["leg_id"]: row["leg_number"] for row in legs}
        journey_started = _parse(snapshot["created_at"])
        assert journey_started is not None
        remaining_legs = [row for row in legs if row["planned_arrival_time"] > snapshot["predicted_at"]]
        remaining_hours = 0.0
        for leg in remaining_legs:
            depart, arrive = _parse(leg["planned_departure_time"]), _parse(leg["planned_arrival_time"])
            assert depart is not None and arrive is not None
            remaining_hours += max(0.0, (arrive - max(cutoff, depart)).total_seconds() / 3600.0)

        route_ids = {row["route_id"] for row in legs}
        facility_ids = {row["origin_facility_id"] for row in legs} | {row["destination_facility_id"] for row in legs}
        route_marks = ",".join("?" for _ in route_ids)
        facility_marks = ",".join("?" for _ in facility_ids)
        where_parts, params = [], []
        if route_ids:
            where_parts.append(f"route_id IN ({route_marks})")
            params.extend(sorted(route_ids))
        if facility_ids:
            where_parts.append(f"facility_id IN ({facility_marks})")
            params.extend(sorted(facility_ids))
        scope = "(" + " OR ".join(where_parts) + ")" if where_parts else "0"
        has_weather_availability = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='WeatherEventAvailability'").fetchone() is not None
        weather_join = "LEFT JOIN WeatherEventAvailability AS wa USING(weather_event_id)" if has_weather_availability else ""
        weather_ingestion = "COALESCE(wa.ingestion_time,w.observed_at)" if has_weather_availability else "w.observed_at"
        weather = connection.execute(f"""
            SELECT COUNT(*) AS observed_count,
              SUM(CASE WHEN w.valid_from<=? AND w.valid_to>? THEN 1 ELSE 0 END) AS active_count,
              MAX(CASE WHEN w.valid_from<=? AND w.valid_to>? THEN w.severity END) AS max_active_severity
            FROM WeatherEvent AS w {weather_join}
            WHERE {scope.replace('route_id', 'w.route_id').replace('facility_id', 'w.facility_id')}
              AND w.observed_at<=? AND {weather_ingestion}<=?
        """, (snapshot["predicted_at"], snapshot["predicted_at"], snapshot["predicted_at"],
              snapshot["predicted_at"], *params, snapshot["predicted_at"], snapshot["predicted_at"])).fetchone()

        forecast = {"n": 0, "severity": None, "precipitation": None, "wind": None}
        has_forecasts = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='WeatherForecast'").fetchone() is not None
        if has_forecasts:
            forecast = connection.execute(f"""SELECT COUNT(*) AS n,MAX(f.severity) AS severity,
                AVG(f.precipitation_mm) AS precipitation,AVG(f.wind_speed_kph) AS wind
                FROM WeatherForecast AS f
                WHERE f.issued_at<=? AND f.ingestion_time<=? AND f.valid_to>?
                  AND {scope.replace('route_id', 'f.route_id').replace('facility_id', 'f.facility_id')}""",
                (snapshot["predicted_at"], snapshot["predicted_at"], snapshot["predicted_at"], *params)).fetchone()

        latest_queues = []
        for facility_id in sorted(facility_ids):
            queue = connection.execute("""SELECT queue_id,facility_id,queue_length,estimated_wait_minutes
                FROM temp.feature_queue_point WHERE facility_id=? AND observed_jd<=julianday(?)
                  AND ingestion_jd<=julianday(?)
                ORDER BY observed_jd DESC,queue_id LIMIT 1""",
                (facility_id, snapshot["predicted_at"], snapshot["predicted_at"])).fetchone()
            if queue is not None:
                latest_queues.append(queue)

        latest_event = events[-1] if events else None
        last_event_time = _parse(latest_event["event_time"]) if latest_event else None
        pickup_events = [row for row in events if row["event_type"] == "picked_up"]
        latest_pickup = _parse(pickup_events[-1]["event_time"]) if pickup_events else None
        weight = snapshot["weight_kg"]
        waits = [row["estimated_wait_minutes"] for row in latest_queues if row["estimated_wait_minutes"] is not None]
        history_total, history_ontime = history_counts.get(snapshot["carrier_id"], [0, 0])
        own_history = history_shipments.get(snapshot["shipment_id"])
        if own_history and own_history[0] == snapshot["carrier_id"]:
            history_total -= 1
            history_ontime -= own_history[1]
        history_missing = int(history_total <= 0)
        # A fixed Beta-style prior avoids missing numeric inputs; count and flag expose warm-up.
        history_rate = (history_ontime + 4 * 0.8) / (history_total + 4) if history_total > 0 else 0.8
        row = {
            "shipment_id": snapshot["shipment_id"],
            "eta_id": snapshot["eta_id"],
            "predicted_at": snapshot["predicted_at"],
            "service_mode": snapshot["service_mode"],
            "service_target_transit_hours": snapshot["service_target_transit_hours"],
            "service_reference_missing": int(snapshot["service_mode"] is None or snapshot["service_target_transit_hours"] is None),
            "carrier_type": snapshot["carrier_type"],
            "carrier_reference_missing": int(snapshot["carrier_type"] is None),
            "carrier_historical_on_time_rate": round(history_rate, 6),
            "carrier_historical_delivery_count": max(0, history_total),
            "carrier_history_missing": history_missing,
            "shipment_priority": snapshot["priority"],
            "shipment_weight_kg": weight,
            "weight_missing": int(weight is None),
            "journey_elapsed_minutes": round(_minutes(cutoff, journey_started), 3),
            "planned_route_distance_km": round(sum(r["distance_km"] for r in legs), 3),
            "planned_route_hours": round(sum(r["planned_transit_hours"] for r in legs), 3),
            "planned_leg_count": len(legs),
            "remaining_scheduled_leg_count": len(remaining_legs),
            "remaining_scheduled_hours": round(remaining_hours, 3),
            "remaining_scheduled_distance_km": round(sum(r["distance_km"] for r in remaining_legs), 3),
            "eligible_event_count": len(events),
            "latest_event_type": latest_event["event_type"] if latest_event else None,
            "latest_event_missing": int(latest_event is None),
            "last_observed_state": _state_from_event(latest_event["event_type"]) if latest_event else None,
            "latest_event_age_minutes": round(_minutes(cutoff, last_event_time), 3) if last_event_time else None,
            "latest_event_leg_number": leg_number.get(latest_event["leg_id"]) if latest_event else None,
            "pickup_scan_seen": int(latest_pickup is not None),
            "minutes_since_pickup_scan": round(_minutes(cutoff, latest_pickup), 3) if latest_pickup else None,
            "weather_observation_count": int(weather["observed_count"] or 0),
            "active_weather_disruption_count": int(weather["active_count"] or 0),
            "max_active_weather_severity": int(weather["max_active_severity"] or 0),
            "forecast_weather_count": int(forecast["n"] or 0),
            "max_forecast_weather_severity": int(forecast["severity"] or 0),
            "forecast_precipitation_mm": round(float(forecast["precipitation"]), 3) if forecast["precipitation"] is not None else None,
            "forecast_wind_speed_kph": round(float(forecast["wind"]), 3) if forecast["wind"] is not None else None,
            "forecast_weather_missing": int(not forecast["n"]),
            "weather_observation_missing": int(not weather["observed_count"]),
            "queue_observation_count": len(latest_queues),
            "max_observed_queue_length": max((r["queue_length"] for r in latest_queues), default=None),
            "mean_observed_wait_minutes": round(sum(waits) / len(waits), 3) if waits else None,
            "queue_wait_missing": int(not waits),
            "queue_observation_missing": int(not latest_queues),
        }
        yield row


def build_feature_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compatibility helper for small tests; production writer uses iterator."""
    return list(iter_feature_rows(connection))


def iter_target_rows(connection: sqlite3.Connection):
    """Return targets separately; actual delivery is read only in this function."""
    connection.row_factory = sqlite3.Row
    rows = connection.execute("""
        SELECT e.eta_id, e.shipment_id, e.predicted_at, s.actual_delivery_time
        FROM ETAEstimate AS e JOIN Shipment AS s ON s.shipment_id=e.shipment_id
        ORDER BY e.predicted_at,e.eta_id
    """)
    for row in rows:
        prediction = _parse(row["predicted_at"])
        actual = _parse(row["actual_delivery_time"])
        eligible = actual is not None and prediction is not None and actual > prediction
        yield {
            "shipment_id": row["shipment_id"],
            "eta_id": row["eta_id"],
            "target_delivery_time": row["actual_delivery_time"],
            "target_remaining_hours": round((actual - prediction).total_seconds() / 3600.0, 6) if eligible else None,
            "target_eligible": int(eligible),
        }


def build_target_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compatibility helper for small tests; production writer uses iterator."""
    return list(iter_target_rows(connection))


def event_cutoff_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Audit event availability over all ETA snapshots without adding it to features."""
    row = connection.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN t.event_time<=e.predicted_at AND t.ingestion_time<=e.predicted_at THEN 1 ELSE 0 END) AS available,
          SUM(CASE WHEN t.event_time<=e.predicted_at AND t.ingestion_time>e.predicted_at THEN 1 ELSE 0 END) AS occurred_but_ingested_late,
          SUM(CASE WHEN t.event_time>e.predicted_at THEN 1 ELSE 0 END) AS event_after_prediction,
          SUM(CASE WHEN t.ingestion_time>e.predicted_at THEN 1 ELSE 0 END) AS ingested_after_prediction
        FROM ETAEstimate AS e JOIN TrackingEvent AS t ON t.shipment_id=e.shipment_id
    """).fetchone()
    return {key: int(row[key] or 0) for key in (
        "total", "available", "occurred_but_ingested_late", "event_after_prediction", "ingested_after_prediction",
    )}
