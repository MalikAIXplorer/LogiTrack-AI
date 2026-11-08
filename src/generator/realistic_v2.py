"""Outcome simulation helpers for the isolated realistic_v2 data profile."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from src.generator.generate import _plus, _stable_seed, _stamp


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _m(value: datetime) -> str:
    return _stamp(value)


def _priority(seed: int, number: int) -> str:
    # Balanced randomized blocks prevent priority from identifying delay labels.
    block, slot = divmod(number - 1, 5)
    values = ["standard", "standard", "low", "high", "critical"]
    random.Random(_stable_seed(seed, "priority_block", block)).shuffle(values)
    return values[slot]


def _carrier_delay_minutes(reliability: float, rng: random.Random) -> int:
    return round((1.0 - reliability) * rng.gammavariate(2.0, 25.0))


def _queue_wait_minutes(estimated_wait: float | None, priority: str, rng: random.Random) -> int:
    if estimated_wait is None:
        return 0
    factor = {"low": 1.20, "standard": 1.0, "high": 0.82, "critical": 0.68}[priority]
    return round(float(estimated_wait) * rng.uniform(.02, .09) * factor)


def _weather_delay(route_id: str, start: datetime, base_end: datetime,
                   windows: list[dict[str, Any]]) -> tuple[int, dict[str, Any] | None]:
    rates = {"rain": .08, "snow": .18, "ice": .24, "fog": .12, "storm": .28, "extreme_heat": .10, "wind": .16}
    maximum, selected = 0, None
    for event in windows:
        if event["route_id"] != route_id:
            continue
        overlap_start = max(start, _dt(event["valid_from"]))
        overlap_end = min(base_end, _dt(event["valid_to"]))
        overlap = max(0, round((overlap_end - overlap_start).total_seconds() / 60))
        impact = round(overlap * rates.get(event["condition"], .05) * int(event["severity"]) / 3)
        if impact > maximum:
            maximum, selected = impact, event
    return maximum, selected


def _detour_extra_minutes(original_hours: float, detour_hours: float, handling_minutes: int) -> int:
    """Only the incremental route time plus handling is charged to the outcome."""
    return max(0, round((detour_hours - original_hours) * 60)) + max(0, int(handling_minutes))


def apply_realistic_outcomes(config: Any, records: dict[str, list[dict[str, Any]]], first: int) -> None:
    """Replace legacy outcome arithmetic with multi-factor, keyed simulation.

    Every random stream is keyed by stable IDs. Detour extra minutes are added
    once to actual leg travel and recorded separately for traceability.
    """
    routes = {row["route_id"]: row for row in records["Route"]}
    route_alternatives: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for route in routes.values():
        route_alternatives.setdefault((route["origin_facility_id"], route["destination_facility_id"], route["mode"]), []).append(route)
    for alternatives in route_alternatives.values():
        alternatives.sort(key=lambda r: (r["route_version"], r["route_id"]))

    weather = records["WeatherEvent"]
    facilities = {row["facility_id"]: row for row in records["Facility"]}
    weather_lag_rngs: dict[str, random.Random] = {}
    for event in weather:
        wrng = random.Random(_stable_seed(config.seed, "weather_ingestion", event["weather_event_id"]))
        ingestion = _dt(event["observed_at"]) + timedelta(minutes=wrng.randint(5, 180))
        records["WeatherEventAvailability"].append({"weather_event_id": event["weather_event_id"], "ingestion_time": _m(ingestion)})
        valid_from = _dt(event["valid_from"])
        issued = valid_from - timedelta(hours=12)
        forecast_ingestion = issued + timedelta(minutes=wrng.randint(1, 40))
        records["WeatherForecast"].append({
            "forecast_id": f"FCST-{event['weather_event_id']}", "route_id": event["route_id"],
            "facility_id": event["facility_id"], "issued_at": _m(issued),
            "ingestion_time": _m(forecast_ingestion), "valid_from": event["valid_from"],
            "valid_to": event["valid_to"], "condition": event["condition"],
            "severity": max(0, min(5, int(event["severity"]) + wrng.choice([-1, 0, 0, 1]))),
            "precipitation_mm": event["precipitation_mm"], "wind_speed_kph": event["wind_speed_kph"],
        })

    delay_rows: list[dict[str, Any]] = []
    existing_delays: dict[str, dict[str, Any]] = {r["shipment_id"]: r for r in records["Shipment"]}
    queue_by_facility: dict[str, list[dict[str, Any]]] = {}
    for queue in records["FacilityQueue"]:
        queue_by_facility.setdefault(queue["facility_id"], []).append(queue)
        qrng = random.Random(_stable_seed(config.seed, "queue_ingestion", queue["queue_id"]))
        records["FacilityQueueAvailability"].append({
            "queue_id": queue["queue_id"],
            "ingestion_time": _m(_dt(queue["observed_at"]) + timedelta(minutes=qrng.randint(1, 150))),
        })

    for shipment in records["Shipment"]:
        shipment_id = shipment["shipment_id"]
        number = int(shipment_id[-6:])
        priority = _priority(config.seed, number)
        shipment["priority"] = priority
        ship_rng = random.Random(_stable_seed(config.seed, "realistic_shipment", shipment_id))
        actual_pickup = _dt(shipment["planned_pickup_time"]) + timedelta(minutes=ship_rng.randint(-10, 25))
        carrier_number = int(shipment["carrier_id"][-6:])
        carrier_rng = random.Random(_stable_seed(config.seed, "carrier_reliability", carrier_number))
        reliability = 0.76 + carrier_rng.random() * 0.22
        legs = sorted((r for r in records["ShipmentLeg"] if r["shipment_id"] == shipment_id), key=lambda r: r["leg_number"])
        plans = [{
            "leg_number": int(leg["leg_number"]), "route_id": leg["route_id"],
            "origin_facility_id": leg["origin_facility_id"], "destination_facility_id": leg["destination_facility_id"],
            "planned_departure_time": leg["planned_departure_time"], "planned_arrival_time": leg["planned_arrival_time"],
        } for leg in legs]
        version = 1
        plan_id = f"PLAN-{shipment_id}-{version:02d}"
        records["ShipmentRoutePlanVersion"].append({
            "plan_version_id": plan_id, "shipment_id": shipment_id, "version_no": version,
            "known_at": shipment["created_at"], "change_reason": "initial_plan",
        })
        for plan in plans:
            records["ShipmentRoutePlanLeg"].append({"plan_leg_id": f"{plan_id}-LEG-{plan['leg_number']:02d}", "plan_version_id": plan_id, **plan})

        clock = actual_pickup
        component_no = 0
        components: list[dict[str, Any]] = []
        shipment_route_changes: list[dict[str, Any]] = []
        final_leg_arrival = actual_pickup
        for leg in legs:
            leg_rng = random.Random(_stable_seed(config.seed, "realistic_leg", leg["leg_id"]))
            original_route = routes[leg["route_id"]]
            route_key = (original_route["origin_facility_id"], original_route["destination_facility_id"], original_route["mode"])
            alternatives = route_alternatives[route_key]
            detour_route = None
            detour_travel = detour_handling = 0
            if len(alternatives) > 1 and leg_rng.random() < 0.18:
                detour_route = alternatives[-1] if alternatives[0]["route_id"] == original_route["route_id"] else alternatives[0]
                detour_handling = leg_rng.randint(15, 90)
                detour_travel = _detour_extra_minutes(float(original_route["planned_transit_hours"]),
                                                       float(detour_route["planned_transit_hours"]), 0)

            queue_wait = 0
            queue_used = None
            eligible_queues = [q for q in queue_by_facility.get(leg["origin_facility_id"], ())
                               if _dt(q["observed_at"]) <= clock and (clock - _dt(q["observed_at"])).total_seconds() <= 8 * 3600]
            if eligible_queues:
                queue_used = max(eligible_queues, key=lambda q: q["observed_at"])
                queue_wait = _queue_wait_minutes(queue_used["estimated_wait_minutes"], priority, leg_rng)
                if queue_wait:
                    component_no += 1
                    components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                        "leg_id": leg["leg_id"], "cause": "facility_queue", "delay_minutes": queue_wait,
                        "weather_event_id": None, "queue_id": queue_used["queue_id"]})
            actual_departure = max(clock, _dt(leg["planned_departure_time"])) + timedelta(minutes=queue_wait)
            planned_minutes = float(original_route["planned_transit_hours"]) * 60
            travel_noise = leg_rng.gauss(0, max(4, planned_minutes * .035))
            carrier_minutes = _carrier_delay_minutes(reliability, leg_rng)
            base_travel_minutes = max(15, round(planned_minutes + travel_noise + carrier_minutes))
            if travel_noise > 0:
                component_no += 1
                components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                    "leg_id": leg["leg_id"], "cause": "travel_variation", "delay_minutes": round(travel_noise),
                    "weather_event_id": None, "queue_id": None})
            if carrier_minutes:
                component_no += 1
                components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                    "leg_id": leg["leg_id"], "cause": "carrier_reliability", "delay_minutes": carrier_minutes,
                    "weather_event_id": None, "queue_id": None})
            base_arrival = actual_departure + timedelta(minutes=base_travel_minutes + detour_travel + detour_handling)
            weather_delay, weather_used = _weather_delay(original_route["route_id"], actual_departure, base_arrival, weather)
            if weather_delay:
                component_no += 1
                components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                    "leg_id": leg["leg_id"], "cause": "weather", "delay_minutes": weather_delay,
                    "weather_event_id": weather_used["weather_event_id"], "queue_id": None})

            customs_delay = 0
            origin = facilities[leg["origin_facility_id"]]
            destination = facilities[leg["destination_facility_id"]]
            if origin["country_code"] != destination["country_code"] and leg_rng.random() < .10:
                customs_delay = round(leg_rng.gammavariate(2.0, 45.0))
                component_no += 1
                components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                    "leg_id": leg["leg_id"], "cause": "customs", "delay_minutes": customs_delay,
                    "weather_event_id": None, "queue_id": None})
            missed_connection = 0
            if int(leg["leg_number"]) > 1 and actual_departure > _dt(leg["planned_departure_time"]) + timedelta(minutes=30) and leg_rng.random() < .18:
                missed_connection = leg_rng.randint(45, 210)
                component_no += 1
                components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                    "leg_id": leg["leg_id"], "cause": "missed_connection", "delay_minutes": missed_connection,
                    "weather_event_id": None, "queue_id": None})

            extra_incident = 0
            incident_rng = random.Random(_stable_seed(config.seed, "priority_risk", leg["leg_id"]))
            incident_p = {"low": .13, "standard": .11, "high": .08, "critical": .06}[priority]
            if incident_rng.random() < incident_p:
                extra_incident = incident_rng.gammavariate(2, 18)
                component_no += 1
                components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                    "leg_id": leg["leg_id"], "cause": "travel_variation", "delay_minutes": round(extra_incident),
                    "weather_event_id": None, "queue_id": None})

            detour_effect = detour_travel + detour_handling
            if detour_route:
                leg["route_id"] = detour_route["route_id"]
                known_at = actual_departure + timedelta(minutes=30)
                records["ShipmentRouteChange"].append({
                    "route_change_id": f"RCH-{shipment_id}-{int(leg['leg_number']):02d}", "shipment_id": shipment_id,
                    "leg_id": leg["leg_id"], "original_route_id": original_route["route_id"],
                    "detour_route_id": detour_route["route_id"], "known_at": _m(known_at),
                    "detour_extra_minutes": detour_travel, "handling_extra_minutes": detour_handling,
                })
                plans[int(leg["leg_number"]) - 1]["route_id"] = detour_route["route_id"]
                version += 1
                plan_id = f"PLAN-{shipment_id}-{version:02d}"
                records["ShipmentRoutePlanVersion"].append({
                    "plan_version_id": plan_id, "shipment_id": shipment_id, "version_no": version,
                    "known_at": _m(known_at), "change_reason": "route_change",
                })
                for plan in plans:
                    records["ShipmentRoutePlanLeg"].append({"plan_leg_id": f"{plan_id}-LEG-{plan['leg_number']:02d}", "plan_version_id": plan_id, **plan})
                if detour_effect:
                    component_no += 1
                    components.append({"component_id": f"CMP-{shipment_id}-{component_no:03d}", "shipment_id": shipment_id,
                        "leg_id": leg["leg_id"], "cause": "route_detour", "delay_minutes": detour_effect,
                        "weather_event_id": None, "queue_id": None})

            arrival = base_arrival + timedelta(minutes=weather_delay + customs_delay + missed_connection + round(extra_incident))
            leg["actual_departure_time"] = _m(actual_departure)
            leg["actual_arrival_time"] = _m(arrival)
            leg["leg_state"] = "arrived"
            final_leg_arrival = arrival
            clock = arrival

        final_rng = random.Random(_stable_seed(config.seed, "delivery_handling", shipment_id))
        delivery_handling = final_rng.randint(5, 45)
        actual_delivery = final_leg_arrival + timedelta(minutes=delivery_handling)
        shipment["actual_pickup_time"] = _m(actual_pickup)
        shipment["actual_delivery_time"] = _m(actual_delivery)
        # Rewrite milestone timestamps to reflect the realized journey, preserving each scan's ingestion lag.
        final_leg_id = f"LEG-{shipment_id}-{len(legs):02d}"
        event_times = {
            "label_created": _dt(shipment["created_at"]), "picked_up": actual_pickup,
            "departed": _dt(legs[0]["actual_departure_time"]),
            "arrived": actual_delivery - timedelta(hours=2), "out_for_delivery": actual_delivery - timedelta(hours=1),
            "delivered": actual_delivery,
        }
        for event in records["TrackingEvent"]:
            if event["shipment_id"] != shipment_id:
                continue
            new_time = event_times.get(event["event_type"], _dt(event["event_time"]))
            lag = max(0, round((_dt(event["ingestion_time"]) - _dt(event["event_time"])).total_seconds() / 60))
            event["event_time"] = _m(new_time)
            event["ingestion_time"] = _m(new_time + timedelta(minutes=lag))
            if event["event_type"] in ("arrived", "out_for_delivery", "delivered"):
                event["leg_id"] = final_leg_id
        tolerance = next(int(r["on_time_tolerance_minutes"]) for r in records["ServiceLevel"] if r["service_level_id"] == shipment["service_level_id"])
        planned_delivery = _dt(shipment["planned_delivery_time"])
        delay_minutes = max(0, int((actual_delivery - planned_delivery).total_seconds() / 60 + 0.0001))
        delayed = int(actual_delivery > planned_delivery + timedelta(minutes=tolerance))
        reason_map = {"weather": "weather", "facility_queue": "facility_congestion", "customs": "customs",
                      "missed_connection": "missed_connection", "carrier_reliability": "carrier_capacity",
                      "route_detour": "operational", "travel_variation": "operational"}
        dominant = max(components, key=lambda c: c["delay_minutes"], default=None)
        reason = reason_map.get(dominant["cause"], "operational") if delayed and dominant else ("operational" if delayed else "none")
        records["ground_truth"] = [r for r in records["ground_truth"] if r["shipment_id"] != shipment_id]
        records["ground_truth"].append({"shipment_id": shipment_id, "actual_delivery_time": _m(actual_delivery),
            "delay_minutes": delay_minutes, "on_time": 1 - delayed, "delayed": delayed, "delay_reason_code": reason,
            "label_rule": "actual_delivery_time > planned_delivery_time + service_tolerance"})
        records["DelayReason"] = [r for r in records["DelayReason"] if r["shipment_id"] != shipment_id]
        if delayed:
            records["DelayReason"].append({"delay_reason_id": "DLY-2026-000000", "shipment_id": shipment_id,
                "leg_id": final_leg_id, "event_id": f"EVT-{shipment_id}-000006", "reason_code": reason,
                "reason_detail": f"Synthetic dominant realized cause: {reason}", "delay_minutes": delay_minutes,
                "identified_at": _m(actual_delivery + timedelta(minutes=5)), "source": "carrier", "is_confirmed": 1})
        records["ShipmentLegOutcomeComponent"].extend(components)

    # Stable delay IDs are assigned in shipment order by the persistence writer.
    records["DelayReason"].sort(key=lambda r: r["shipment_id"])

