# Data dictionary

All timestamp fields use ISO 8601 UTC text (`YYYY-MM-DDTHH:MM:SSZ`). Identifiers are text primary keys unless specified otherwise. Optional means SQL `NULL` is permitted. Required fields have `NOT NULL` (or are primary keys). “Unique” refers to a database uniqueness constraint; otherwise only the primary key is unique.

## Deterministic ID formats

| Entity | Format | Example |
|---|---|---|
| Shipment | `SHP-2026-000001` (year plus six-digit sequence) | `SHP-2026-000001` |
| ShipmentLeg | `LEG-<shipment-id>-01` (two-digit leg ordinal) | `LEG-SHP-2026-000001-01` |
| TrackingEvent | `EVT-<shipment-id>-000001` (six-digit per-shipment sequence) | `EVT-SHP-2026-000001-000001` |
| ETAEstimate | `ETA-<shipment-id>-<timestamp>` (UTC timestamp, `YYYYMMDDTHHMMSSZ`) | `ETA-SHP-2026-000001-20261022T120000Z` |
| DelayReason | `DLY-2026-000001` (year plus six-digit sequence) | `DLY-2026-000001` |

Other entity IDs are stable opaque text identifiers with entity prefixes (for example `CAR-000001`, `FAC-000001`, `RTE-000001`, `SLV-000001`, `FQ-000001`, `WTH-000001`).

## Shipment

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| shipment_id | TEXT | Yes | `SHP-YYYY-NNNNNN` | PK | Match deterministic format |
| external_reference | TEXT | No | External system identifier | Yes when present | No duplicate external references |
| carrier_id | TEXT | Yes | Carrier ID | No | Must reference Carrier |
| route_id | TEXT | Yes | Route ID | No | Must reference Route |
| service_level_id | TEXT | Yes | Service level ID | No | Must reference ServiceLevel |
| origin_facility_id | TEXT | Yes | Facility ID | No | Must reference Facility |
| destination_facility_id | TEXT | Yes | Facility ID | No | Must reference Facility |
| created_at | TEXT | Yes | UTC timestamp | No | Valid ISO-8601 timestamp |
| planned_pickup_time | TEXT | Yes | UTC timestamp | No | Earlier than planned delivery |
| planned_delivery_time | TEXT | Yes | UTC timestamp | No | Later than planned pickup |
| actual_pickup_time | TEXT | No | UTC timestamp | No | Valid timestamp |
| actual_delivery_time | TEXT | No | UTC timestamp | No | Not earlier than actual pickup when both exist |
| lifecycle_state | TEXT | Yes | `created`, `booked`, `in_transit`, `out_for_delivery`, `delivered`, `exception`, `cancelled`, `returned` | No | Must be in allowed set |
| priority | TEXT | Yes | `low`, `standard`, `high`, `critical`; default `standard` | No | Must be in allowed set |
| weight_kg | REAL | No | kg | No | Positive when present |
| declared_value | REAL | No | Currency units | No | Nonnegative when present |
| currency_code | TEXT | No | ISO 4217-style three-letter code | No | Length 3 when present |

## ShipmentLeg

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| leg_id | TEXT | Yes | `LEG-<shipment-id>-NN` | PK | Match deterministic format |
| shipment_id | TEXT | Yes | Shipment ID | No | Must reference Shipment |
| leg_number | INTEGER | Yes | Positive ordinal | Per-shipment with shipment_id | Greater than 0 |
| route_id | TEXT | Yes | Route ID | No | Must reference Route |
| carrier_id | TEXT | Yes | Carrier ID | No | Must reference Carrier |
| origin_facility_id | TEXT | Yes | Facility ID | No | Must reference Facility |
| destination_facility_id | TEXT | Yes | Facility ID | No | Must reference Facility |
| planned_departure_time | TEXT | Yes | UTC timestamp | No | Earlier than planned arrival |
| planned_arrival_time | TEXT | Yes | UTC timestamp | No | Later than planned departure |
| actual_departure_time | TEXT | No | UTC timestamp | No | Valid timestamp |
| actual_arrival_time | TEXT | No | UTC timestamp | No | Not earlier than actual departure when both exist |
| leg_state | TEXT | Yes | `planned`, `ready`, `in_transit`, `arrived`, `cancelled`, `exception` | No | Must be in allowed set |

## Carrier

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| carrier_id | TEXT | Yes | Stable carrier ID | PK | Nonempty |
| carrier_name | TEXT | Yes | Nonempty name | Yes | Trimmed value not blank |
| carrier_type | TEXT | Yes | `parcel`, `ltl`, `ftl`, `air`, `ocean`, `rail`, `last_mile` | No | Must be in allowed set |
| home_country | TEXT | Yes | ISO 3166-1 alpha-2 | No | Exactly 2 characters |
| active | INTEGER | Yes | Boolean `0` / `1`; default 1 | No | Must be 0 or 1 |
| effective_from | TEXT | Yes | UTC timestamp/date | No | Earlier than effective_to if present |
| effective_to | TEXT | No | UTC timestamp/date | No | Must be later than effective_from |

## Route

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| route_id | TEXT | Yes | Stable route ID | PK | Nonempty |
| origin_facility_id | TEXT | Yes | Facility ID | No | Must reference Facility; differs from destination |
| destination_facility_id | TEXT | Yes | Facility ID | No | Must reference Facility; differs from origin |
| mode | TEXT | Yes | `parcel`, `ltl`, `ftl`, `air`, `ocean`, `rail`, `last_mile` | No | Must be in allowed set |
| distance_km | REAL | Yes | km | No | Greater than zero |
| planned_transit_hours | REAL | Yes | hours | No | Greater than zero |
| route_version | INTEGER | Yes | Positive integer; default 1 | Within origin/destination/mode | Greater than zero |
| effective_from | TEXT | Yes | UTC timestamp/date | No | Earlier than effective_to if present |
| effective_to | TEXT | No | UTC timestamp/date | No | Must be later than effective_from |

## TrackingEvent

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| event_id | TEXT | Yes | `EVT-<shipment-id>-NNNNNN` | PK | Match deterministic format |
| shipment_id | TEXT | Yes | Shipment ID | Pair with event_id | Must reference Shipment |
| leg_id | TEXT | No | ShipmentLeg ID | No | Must reference ShipmentLeg when present |
| facility_id | TEXT | No | Facility ID | No | Must reference Facility when present |
| event_type | TEXT | Yes | `label_created`, `picked_up`, `departed`, `arrived`, `in_facility`, `out_for_delivery`, `delivered`, `delay`, `exception`, `customs_clearance`, `returned` | No | Must be in allowed set |
| event_time | TEXT | Yes | UTC timestamp when event occurred | No | Valid ISO-8601 timestamp |
| ingestion_time | TEXT | Yes | UTC timestamp when platform received event | No | Valid timestamp; may be later than event_time |
| source | TEXT | Yes | `carrier`, `facility`, `device`, `customer`, `system` | No | Must be in allowed set |
| status_detail | TEXT | No | Free text | No | Length/quality checks at ingestion |
| latitude | REAL | No | Decimal degrees | No | -90 to 90 |
| longitude | REAL | No | Decimal degrees | No | -180 to 180 |

## Facility

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| facility_id | TEXT | Yes | Stable facility ID | PK | Nonempty |
| facility_name | TEXT | Yes | Nonempty name | No | Trimmed value not blank |
| facility_type | TEXT | Yes | `warehouse`, `hub`, `port`, `airport`, `cross_dock`, `customer` | No | Must be in allowed set |
| country_code | TEXT | Yes | ISO 3166-1 alpha-2 | No | Exactly 2 characters |
| timezone | TEXT | Yes | IANA timezone name | No | Validate with timezone database in application |
| latitude | REAL | Yes | Decimal degrees | No | -90 to 90 |
| longitude | REAL | Yes | Decimal degrees | No | -180 to 180 |
| active | INTEGER | Yes | Boolean `0` / `1`; default 1 | No | Must be 0 or 1 |
| effective_from | TEXT | Yes | UTC timestamp/date | No | Earlier than effective_to if present |
| effective_to | TEXT | No | UTC timestamp/date | No | Must be later than effective_from |

## FacilityQueue

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| queue_id | TEXT | Yes | Stable queue observation ID | PK | Nonempty |
| facility_id | TEXT | Yes | Facility ID | Unique with observed_at, source | Must reference Facility |
| observed_at | TEXT | Yes | UTC timestamp | Unique with facility_id, source | Valid timestamp |
| queue_length | INTEGER | Yes | Number of queued units | No | Integer >= 0 |
| estimated_wait_minutes | REAL | No | minutes | No | >= 0 when present |
| capacity | INTEGER | No | Number of units | No | > 0 when present |
| source | TEXT | Yes | `sensor`, `facility`, `system` | Unique with facility_id, observed_at | Must be in allowed set |

## WeatherEvent

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| weather_event_id | TEXT | Yes | Stable weather observation ID | PK | Nonempty |
| facility_id | TEXT | No | Facility ID | No | Must reference Facility when present |
| route_id | TEXT | No | Route ID | No | Must reference Route when present |
| observed_at | TEXT | Yes | UTC timestamp observation/forecast issued | No | Valid timestamp |
| valid_from | TEXT | Yes | UTC timestamp | No | Earlier than valid_to |
| valid_to | TEXT | Yes | UTC timestamp | No | Later than valid_from |
| condition | TEXT | Yes | `clear`, `cloudy`, `rain`, `snow`, `ice`, `fog`, `storm`, `extreme_heat`, `wind` | No | Must be in allowed set |
| severity | INTEGER | Yes | Ordinal 0 to 5 | No | Inclusive range 0-5 |
| precipitation_mm | REAL | No | mm | No | >= 0 when present |
| wind_speed_kph | REAL | No | km/h | No | >= 0 when present |
| source | TEXT | Yes | Provider identifier | No | Nonempty |

## ServiceLevel

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| service_level_id | TEXT | Yes | Stable service level ID | PK | Nonempty |
| service_name | TEXT | Yes | Nonempty service name | Yes | Trimmed value not blank |
| mode | TEXT | Yes | `parcel`, `ltl`, `ftl`, `air`, `ocean`, `rail`, `last_mile` | No | Must be in allowed set |
| target_transit_hours | REAL | Yes | hours | No | Greater than zero |
| on_time_tolerance_minutes | INTEGER | Yes | minutes; default 0 | No | >= 0 |
| active | INTEGER | Yes | Boolean `0` / `1`; default 1 | No | Must be 0 or 1 |
| effective_from | TEXT | Yes | UTC timestamp/date | No | Earlier than effective_to if present |
| effective_to | TEXT | No | UTC timestamp/date | No | Must be later than effective_from |

## ETAEstimate

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| eta_id | TEXT | Yes | `ETA-<shipment-id>-YYYYMMDDTHHMMSSZ` | PK | Timestamp segment equals prediction time |
| shipment_id | TEXT | Yes | Shipment ID | No | Must reference Shipment |
| predicted_at | TEXT | Yes | UTC prediction timestamp | Unique with shipment_id, model_version | Valid timestamp |
| estimated_delivery_time | TEXT | Yes | UTC predicted delivery timestamp | No | Valid timestamp |
| prediction_horizon_hours | REAL | Yes | hours from predicted_at to estimated delivery | No | >= 0 |
| model_version | TEXT | Yes | Version identifier | Unique with shipment_id, predicted_at | Nonempty |
| prediction_type | TEXT | Yes | `baseline`, `model`, `manual` | No | Must be in allowed set |
| confidence | REAL | No | Probability from 0 to 1 | No | Inclusive range when present |
| actual_delivery_time_at_prediction | TEXT | No | Actual delivery known at prediction time, UTC | No | For audit; valid timestamp if present |

## DelayReason

| Field | Type | Req. | Allowed values / units | Unique | Validation |
|---|---|---:|---|---|---|
| delay_reason_id | TEXT | Yes | `DLY-YYYY-NNNNNN` | PK | Match deterministic format |
| shipment_id | TEXT | Yes | Shipment ID | No | Must reference Shipment |
| leg_id | TEXT | No | ShipmentLeg ID | No | Must reference ShipmentLeg when present |
| event_id | TEXT | No | TrackingEvent ID | No | Must reference TrackingEvent when present |
| reason_code | TEXT | Yes | `weather`, `facility_congestion`, `carrier_capacity`, `mechanical`, `customs`, `address_issue`, `missed_connection`, `operational`, `unknown`, `other` | No | Must be in allowed set |
| reason_detail | TEXT | No | Free text | No | Length/quality checks at ingestion |
| delay_minutes | INTEGER | No | minutes | No | >= 0 when present |
| identified_at | TEXT | Yes | UTC timestamp | No | Valid timestamp |
| source | TEXT | Yes | `event`, `carrier`, `model`, `analyst`, `customer` | No | Must be in allowed set |
| is_confirmed | INTEGER | Yes | Boolean `0` / `1`; default 0 | No | Must be 0 or 1 |

## Temporal availability and leakage controls

`event_time` is when an operational event occurred in the real world. `ingestion_time` is when LogiTrack first received it. For a prediction made at time `t`, only event records with `ingestion_time <= t` are available, even if an event with an earlier `event_time` arrived late. Historical feature reconstruction must apply this as-of ingestion cutoff. Weather and queue features must likewise use observations/forecasts whose `observed_at <= t` and validity intervals cover the relevant time; later-arriving revisions are unavailable. Actual pickup/delivery fields, post-prediction delay reasons, and later lifecycle transitions are labels or audit data, not predictors at earlier times. `ETAEstimate.actual_delivery_time_at_prediction` is an explicit audit snapshot and must remain null unless the value was genuinely known at prediction time.

## Generator label and anomaly rules

The generator computes delivery delay as the elapsed minutes from `planned_delivery_time` to `actual_delivery_time`. A shipment is labelled delayed only when this exceeds the applicable service-level `on_time_tolerance_minutes`; otherwise it is on time. Non-disrupted transit uses a seeded schedule variance from 45 minutes early to 25 minutes late. Disrupted transit receives a reason-specific delay (weather 300, facility congestion 150, carrier capacity 240, mechanical 210, customs 420, or missed connection 360 minutes) plus a seeded 0-90 minutes. The delay reason is recorded against the delivered event. These rules create labels from operational schedule and disruption mechanisms rather than drawing labels independently.

Exactly four audit examples are generated separately in `AnomalyAudit` and `ground_truth.csv`: ingestion time mistaken for event time, impossible route ordering, future-event use in an ETA input snapshot, and a delivered shipment reported as in transit. `AnomalyAudit` uses a typed target (`target_entity_type`, `target_record_id`) which the validator resolves against the corresponding table; it intentionally has no generic polymorphic foreign key. `AnomalyRouteSequence`, `AnomalyStateObservation`, and `AnomalyETAInputEvidence` hold traceable, foreign-keyed anomaly evidence outside operational source tables. The route case stores a reversed sequence of real leg IDs; the state case records an in-transit observation after the preserved delivered event; the ETA case records a deliberately included event whose event and ingestion times are after prediction time. None overwrites its source row. Exclude `ground_truth.csv`, `AnomalyAudit`, and all anomaly evidence tables from model input construction.

## Supporting tables

`CarrierCalendar(calendar_id, carrier_id, calendar_date, is_operating, capacity_factor)` records daily carrier operations; `FacilityCalendar(calendar_id, facility_id, calendar_date, is_open, capacity_factor)` records daily facility operations. Both are keyed by deterministic calendar IDs and enforce unique entity/date pairs. `AnomalyAudit(anomaly_id, shipment_id, target_entity_type, target_record_id, anomaly_type, affected_field, expected_value, observed_value, detected_at, severity)` records typed issue annotations. `AnomalyRouteSequence(sequence_record_id, anomaly_id, shipment_id, sequence_position, leg_id, expected_position)` records the intentionally reversed leg order. `AnomalyStateObservation(state_observation_id, anomaly_id, shipment_id, source_event_id, observed_at, observed_state)` records a later contradictory state observation. `AnomalyETAInputEvidence(input_snapshot_id, anomaly_id, shipment_id, eta_id, event_id, predicted_at, event_time, ingestion_time, feature_name, feature_value, intentionally_included)` records the deliberately leaked ETA feature input. These are not members of the 11 core entities.
