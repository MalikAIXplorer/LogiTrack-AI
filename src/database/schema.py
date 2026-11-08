"""SQLite schema for LogiTrack AI's 11 core entities."""

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Carrier (
    carrier_id TEXT PRIMARY KEY,
    carrier_name TEXT NOT NULL UNIQUE CHECK (length(trim(carrier_name)) > 0),
    carrier_type TEXT NOT NULL CHECK (carrier_type IN ('parcel', 'ltl', 'ftl', 'air', 'ocean', 'rail', 'last_mile')),
    home_country TEXT NOT NULL CHECK (length(home_country) = 2),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS Facility (
    facility_id TEXT PRIMARY KEY,
    facility_name TEXT NOT NULL CHECK (length(trim(facility_name)) > 0),
    facility_type TEXT NOT NULL CHECK (facility_type IN ('warehouse', 'hub', 'port', 'airport', 'cross_dock', 'customer')),
    country_code TEXT NOT NULL CHECK (length(country_code) = 2),
    timezone TEXT NOT NULL,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS ServiceLevel (
    service_level_id TEXT PRIMARY KEY,
    service_name TEXT NOT NULL UNIQUE CHECK (length(trim(service_name)) > 0),
    mode TEXT NOT NULL CHECK (mode IN ('parcel', 'ltl', 'ftl', 'air', 'ocean', 'rail', 'last_mile')),
    target_transit_hours REAL NOT NULL CHECK (target_transit_hours > 0),
    on_time_tolerance_minutes INTEGER NOT NULL DEFAULT 0 CHECK (on_time_tolerance_minutes >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS Route (
    route_id TEXT PRIMARY KEY,
    origin_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    destination_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    mode TEXT NOT NULL CHECK (mode IN ('parcel', 'ltl', 'ftl', 'air', 'ocean', 'rail', 'last_mile')),
    distance_km REAL NOT NULL CHECK (distance_km > 0),
    planned_transit_hours REAL NOT NULL CHECK (planned_transit_hours > 0),
    route_version INTEGER NOT NULL DEFAULT 1 CHECK (route_version > 0),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    CHECK (origin_facility_id <> destination_facility_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    UNIQUE (origin_facility_id, destination_facility_id, mode, route_version)
);

CREATE TABLE IF NOT EXISTS Shipment (
    shipment_id TEXT PRIMARY KEY,
    external_reference TEXT UNIQUE,
    carrier_id TEXT NOT NULL REFERENCES Carrier(carrier_id),
    route_id TEXT NOT NULL REFERENCES Route(route_id),
    service_level_id TEXT NOT NULL REFERENCES ServiceLevel(service_level_id),
    origin_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    destination_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    created_at TEXT NOT NULL,
    planned_pickup_time TEXT NOT NULL,
    planned_delivery_time TEXT NOT NULL,
    actual_pickup_time TEXT,
    actual_delivery_time TEXT,
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('created', 'booked', 'in_transit', 'out_for_delivery', 'delivered', 'exception', 'cancelled', 'returned')),
    priority TEXT NOT NULL DEFAULT 'standard' CHECK (priority IN ('low', 'standard', 'high', 'critical')),
    weight_kg REAL CHECK (weight_kg IS NULL OR weight_kg > 0),
    declared_value REAL CHECK (declared_value IS NULL OR declared_value >= 0),
    currency_code TEXT CHECK (currency_code IS NULL OR length(currency_code) = 3),
    CHECK (planned_delivery_time > planned_pickup_time),
    CHECK (actual_delivery_time IS NULL OR actual_pickup_time IS NULL OR actual_delivery_time >= actual_pickup_time)
);

CREATE TABLE IF NOT EXISTS ShipmentLeg (
    leg_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    leg_number INTEGER NOT NULL CHECK (leg_number > 0),
    route_id TEXT NOT NULL REFERENCES Route(route_id),
    carrier_id TEXT NOT NULL REFERENCES Carrier(carrier_id),
    origin_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    destination_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    planned_departure_time TEXT NOT NULL,
    planned_arrival_time TEXT NOT NULL,
    actual_departure_time TEXT,
    actual_arrival_time TEXT,
    leg_state TEXT NOT NULL CHECK (leg_state IN ('planned', 'ready', 'in_transit', 'arrived', 'cancelled', 'exception')),
    CHECK (planned_arrival_time > planned_departure_time),
    CHECK (actual_arrival_time IS NULL OR actual_departure_time IS NULL OR actual_arrival_time >= actual_departure_time),
    UNIQUE (shipment_id, leg_number)
);

CREATE TABLE IF NOT EXISTS TrackingEvent (
    event_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    leg_id TEXT REFERENCES ShipmentLeg(leg_id),
    facility_id TEXT REFERENCES Facility(facility_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('label_created', 'picked_up', 'departed', 'arrived', 'in_facility', 'out_for_delivery', 'delivered', 'delay', 'exception', 'customs_clearance', 'returned')),
    event_time TEXT NOT NULL,
    ingestion_time TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('carrier', 'facility', 'device', 'customer', 'system')),
    status_detail TEXT,
    latitude REAL CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    longitude REAL CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
    UNIQUE (shipment_id, event_id)
);

CREATE TABLE IF NOT EXISTS FacilityQueue (
    queue_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    observed_at TEXT NOT NULL,
    queue_length INTEGER NOT NULL CHECK (queue_length >= 0),
    estimated_wait_minutes REAL CHECK (estimated_wait_minutes IS NULL OR estimated_wait_minutes >= 0),
    capacity INTEGER CHECK (capacity IS NULL OR capacity > 0),
    source TEXT NOT NULL CHECK (source IN ('sensor', 'facility', 'system')),
    UNIQUE (facility_id, observed_at, source)
);

CREATE TABLE IF NOT EXISTS WeatherEvent (
    weather_event_id TEXT PRIMARY KEY,
    facility_id TEXT REFERENCES Facility(facility_id),
    route_id TEXT REFERENCES Route(route_id),
    observed_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    condition TEXT NOT NULL CHECK (condition IN ('clear', 'cloudy', 'rain', 'snow', 'ice', 'fog', 'storm', 'extreme_heat', 'wind')),
    severity INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 5),
    precipitation_mm REAL CHECK (precipitation_mm IS NULL OR precipitation_mm >= 0),
    wind_speed_kph REAL CHECK (wind_speed_kph IS NULL OR wind_speed_kph >= 0),
    source TEXT NOT NULL,
    CHECK ((facility_id IS NOT NULL) OR (route_id IS NOT NULL)),
    CHECK (valid_to > valid_from)
);

CREATE TABLE IF NOT EXISTS ETAEstimate (
    eta_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    predicted_at TEXT NOT NULL,
    estimated_delivery_time TEXT NOT NULL,
    prediction_horizon_hours REAL NOT NULL CHECK (prediction_horizon_hours >= 0),
    model_version TEXT NOT NULL,
    prediction_type TEXT NOT NULL CHECK (prediction_type IN ('baseline', 'model', 'manual')),
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    actual_delivery_time_at_prediction TEXT,
    UNIQUE (shipment_id, predicted_at, model_version)
);

CREATE TABLE IF NOT EXISTS DelayReason (
    delay_reason_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    leg_id TEXT REFERENCES ShipmentLeg(leg_id),
    event_id TEXT REFERENCES TrackingEvent(event_id),
    reason_code TEXT NOT NULL CHECK (reason_code IN ('weather', 'facility_congestion', 'carrier_capacity', 'mechanical', 'customs', 'address_issue', 'missed_connection', 'operational', 'unknown', 'other')),
    reason_detail TEXT,
    delay_minutes INTEGER CHECK (delay_minutes IS NULL OR delay_minutes >= 0),
    identified_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('event', 'carrier', 'model', 'analyst', 'customer')),
    is_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (is_confirmed IN (0, 1))
);

-- Supporting records are kept outside the 11 core business entities.
CREATE TABLE IF NOT EXISTS CarrierCalendar (
    calendar_id TEXT PRIMARY KEY,
    carrier_id TEXT NOT NULL REFERENCES Carrier(carrier_id) ON DELETE CASCADE,
    calendar_date TEXT NOT NULL,
    is_operating INTEGER NOT NULL CHECK (is_operating IN (0, 1)),
    capacity_factor REAL NOT NULL CHECK (capacity_factor BETWEEN 0 AND 2),
    UNIQUE (carrier_id, calendar_date)
);

CREATE TABLE IF NOT EXISTS FacilityCalendar (
    calendar_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES Facility(facility_id) ON DELETE CASCADE,
    calendar_date TEXT NOT NULL,
    is_open INTEGER NOT NULL CHECK (is_open IN (0, 1)),
    capacity_factor REAL NOT NULL CHECK (capacity_factor BETWEEN 0 AND 2),
    UNIQUE (facility_id, calendar_date)
);

-- Versioned point-in-time availability and realistic_v2 outcome evidence.
CREATE TABLE IF NOT EXISTS WeatherEventAvailability (
    weather_event_id TEXT PRIMARY KEY REFERENCES WeatherEvent(weather_event_id) ON DELETE CASCADE,
    ingestion_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS FacilityQueueAvailability (
    queue_id TEXT PRIMARY KEY REFERENCES FacilityQueue(queue_id) ON DELETE CASCADE,
    ingestion_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS WeatherForecast (
    forecast_id TEXT PRIMARY KEY,
    route_id TEXT REFERENCES Route(route_id),
    facility_id TEXT REFERENCES Facility(facility_id),
    issued_at TEXT NOT NULL,
    ingestion_time TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    condition TEXT NOT NULL CHECK (condition IN ('clear', 'cloudy', 'rain', 'snow', 'ice', 'fog', 'storm', 'extreme_heat', 'wind')),
    severity INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 5),
    precipitation_mm REAL CHECK (precipitation_mm IS NULL OR precipitation_mm >= 0),
    wind_speed_kph REAL CHECK (wind_speed_kph IS NULL OR wind_speed_kph >= 0),
    CHECK (route_id IS NOT NULL OR facility_id IS NOT NULL),
    CHECK (valid_to > valid_from)
);
CREATE TABLE IF NOT EXISTS ShipmentRoutePlanVersion (
    plan_version_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    known_at TEXT NOT NULL,
    change_reason TEXT NOT NULL CHECK (change_reason IN ('initial_plan', 'route_change')),
    UNIQUE (shipment_id, version_no)
);
CREATE TABLE IF NOT EXISTS ShipmentRoutePlanLeg (
    plan_leg_id TEXT PRIMARY KEY,
    plan_version_id TEXT NOT NULL REFERENCES ShipmentRoutePlanVersion(plan_version_id) ON DELETE CASCADE,
    leg_number INTEGER NOT NULL CHECK (leg_number > 0),
    route_id TEXT NOT NULL REFERENCES Route(route_id),
    origin_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    destination_facility_id TEXT NOT NULL REFERENCES Facility(facility_id),
    planned_departure_time TEXT NOT NULL,
    planned_arrival_time TEXT NOT NULL,
    UNIQUE (plan_version_id, leg_number),
    CHECK (planned_arrival_time > planned_departure_time)
);
CREATE TABLE IF NOT EXISTS ShipmentRouteChange (
    route_change_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    leg_id TEXT NOT NULL REFERENCES ShipmentLeg(leg_id) ON DELETE CASCADE,
    original_route_id TEXT NOT NULL REFERENCES Route(route_id),
    detour_route_id TEXT NOT NULL REFERENCES Route(route_id),
    known_at TEXT NOT NULL,
    detour_extra_minutes INTEGER NOT NULL CHECK (detour_extra_minutes >= 0),
    handling_extra_minutes INTEGER NOT NULL CHECK (handling_extra_minutes >= 0)
);
CREATE TABLE IF NOT EXISTS ShipmentLegOutcomeComponent (
    component_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    leg_id TEXT NOT NULL REFERENCES ShipmentLeg(leg_id) ON DELETE CASCADE,
    cause TEXT NOT NULL CHECK (cause IN ('travel_variation', 'carrier_reliability', 'weather', 'facility_queue', 'customs', 'missed_connection', 'route_detour')),
    delay_minutes INTEGER NOT NULL CHECK (delay_minutes >= 0),
    weather_event_id TEXT REFERENCES WeatherEvent(weather_event_id),
    queue_id TEXT REFERENCES FacilityQueue(queue_id)
);
CREATE INDEX IF NOT EXISTS idx_weather_availability ON WeatherEventAvailability(ingestion_time, weather_event_id);
CREATE INDEX IF NOT EXISTS idx_queue_availability ON FacilityQueueAvailability(ingestion_time, queue_id);
CREATE INDEX IF NOT EXISTS idx_forecast_availability ON WeatherForecast(issued_at, ingestion_time, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_plan_version_asof ON ShipmentRoutePlanVersion(shipment_id, known_at DESC, version_no DESC);
CREATE INDEX IF NOT EXISTS idx_delivery_history_asof ON TrackingEvent(event_type, ingestion_time, event_time, shipment_id);

CREATE TABLE IF NOT EXISTS AnomalyAudit (
    anomaly_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    target_entity_type TEXT NOT NULL CHECK (target_entity_type IN ('Shipment', 'ShipmentLeg', 'TrackingEvent', 'ETAEstimate')),
    target_record_id TEXT NOT NULL,
    anomaly_type TEXT NOT NULL CHECK (anomaly_type IN (
        'ingestion_time_confused_with_event_time', 'impossible_route_order',
        'future_event_leakage_in_eta', 'delivered_marked_in_transit')),
    affected_field TEXT NOT NULL,
    expected_value TEXT NOT NULL,
    observed_value TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    UNIQUE (shipment_id, anomaly_type)
);

-- These evidence records deliberately live outside operational model inputs.
-- Their foreign keys preserve source history while representing labelled anomalies.
CREATE TABLE IF NOT EXISTS AnomalyRouteSequence (
    sequence_record_id TEXT PRIMARY KEY,
    anomaly_id TEXT NOT NULL REFERENCES AnomalyAudit(anomaly_id) ON DELETE CASCADE,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    sequence_position INTEGER NOT NULL CHECK (sequence_position > 0),
    leg_id TEXT NOT NULL REFERENCES ShipmentLeg(leg_id),
    expected_position INTEGER NOT NULL CHECK (expected_position > 0),
    UNIQUE (anomaly_id, sequence_position),
    UNIQUE (anomaly_id, leg_id)
);

CREATE TABLE IF NOT EXISTS AnomalyStateObservation (
    state_observation_id TEXT PRIMARY KEY,
    anomaly_id TEXT NOT NULL UNIQUE REFERENCES AnomalyAudit(anomaly_id) ON DELETE CASCADE,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL REFERENCES TrackingEvent(event_id),
    observed_at TEXT NOT NULL,
    observed_state TEXT NOT NULL CHECK (observed_state IN ('created', 'booked', 'in_transit', 'out_for_delivery', 'delivered', 'exception', 'cancelled', 'returned'))
);

CREATE TABLE IF NOT EXISTS AnomalyETAInputEvidence (
    input_snapshot_id TEXT PRIMARY KEY,
    anomaly_id TEXT NOT NULL UNIQUE REFERENCES AnomalyAudit(anomaly_id) ON DELETE CASCADE,
    shipment_id TEXT NOT NULL REFERENCES Shipment(shipment_id) ON DELETE CASCADE,
    eta_id TEXT NOT NULL REFERENCES ETAEstimate(eta_id),
    event_id TEXT NOT NULL REFERENCES TrackingEvent(event_id),
    predicted_at TEXT NOT NULL,
    event_time TEXT NOT NULL,
    ingestion_time TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    feature_value TEXT NOT NULL,
    intentionally_included INTEGER NOT NULL CHECK (intentionally_included = 1),
    CHECK (event_time > predicted_at OR ingestion_time > predicted_at)
);

CREATE INDEX IF NOT EXISTS idx_shipment_state_planned_delivery ON Shipment(lifecycle_state, planned_delivery_time);
CREATE INDEX IF NOT EXISTS idx_shipment_route ON Shipment(route_id);
CREATE INDEX IF NOT EXISTS idx_leg_shipment_number ON ShipmentLeg(shipment_id, leg_number);
CREATE INDEX IF NOT EXISTS idx_event_shipment_event_time ON TrackingEvent(shipment_id, event_time);
CREATE INDEX IF NOT EXISTS idx_event_ingestion_time ON TrackingEvent(ingestion_time);
CREATE INDEX IF NOT EXISTS idx_queue_facility_observed ON FacilityQueue(facility_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_weather_validity ON WeatherEvent(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_eta_shipment_predicted ON ETAEstimate(shipment_id, predicted_at);
CREATE INDEX IF NOT EXISTS idx_delay_shipment_identified ON DelayReason(shipment_id, identified_at);
"""

CORE_TABLES = (
    "Shipment", "ShipmentLeg", "Carrier", "Route", "TrackingEvent", "Facility",
    "FacilityQueue", "WeatherEvent", "ServiceLevel", "ETAEstimate", "DelayReason",
)
