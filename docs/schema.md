# Database schema

The SQLite schema contains exactly 11 core entity tables: `Shipment`, `ShipmentLeg`, `Carrier`, `Route`, `TrackingEvent`, `Facility`, `FacilityQueue`, `WeatherEvent`, `ServiceLevel`, `ETAEstimate`, and `DelayReason`. Supporting tables are `CarrierCalendar`, `FacilityCalendar`, `AnomalyAudit`, `AnomalyRouteSequence`, `AnomalyStateObservation`, and `AnomalyETAInputEvidence`; they do not alter the 11 core business entities. Foreign-key checks are enabled by the initializer on its connection. SQLite requires `PRAGMA foreign_keys = ON` on every new connection, so consumers must enable it per connection too.

All timestamps are ISO 8601 text; generators and consumers should store UTC with a `Z` suffix. Effective-dated reference entities (`Carrier`, `Facility`, `ServiceLevel`, `Route`) use inclusive `effective_from` and exclusive `effective_to` semantics. `Shipment.lifecycle_state` and `ShipmentLeg.leg_state` constrain the operational lifecycle.

## Relationships and cardinalities

| Relationship | Cardinality | Enforcement |
|---|---|---|
| Carrier to Shipment | One carrier to many shipments | `Shipment.carrier_id` |
| Route to Shipment | One route to many shipments | `Shipment.route_id` |
| ServiceLevel to Shipment | One service level to many shipments | `Shipment.service_level_id` |
| Facility to Shipment origin/destination | One facility to many shipments in each role | Two shipment foreign keys |
| Facility to Route origin/destination | One facility to many routes in each role | Two route foreign keys |
| Shipment to ShipmentLeg | One shipment to zero or many legs | `ON DELETE CASCADE`; unique leg number per shipment |
| Route, Carrier, Facility to ShipmentLeg | Each referenced row can serve many legs | Leg foreign keys |
| Shipment to TrackingEvent | One shipment to zero or many events | `ON DELETE CASCADE` |
| ShipmentLeg / Facility to TrackingEvent | Each leg/facility can be referenced by many events; event links are optional | Nullable foreign keys |
| Facility to FacilityQueue | One facility to many queue observations | Facility foreign key; unique facility/time/source |
| Facility / Route to WeatherEvent | Each can have many weather records; each weather record must reference at least one | Nullable foreign keys plus CHECK |
| Shipment to ETAEstimate | One shipment to zero or many estimates | `ON DELETE CASCADE`; one model/version estimate per prediction time |
| Shipment to DelayReason | One shipment to zero or many delay reasons | `ON DELETE CASCADE` |
| ShipmentLeg / TrackingEvent to DelayReason | Each may support many reasons; links are optional | Nullable foreign keys |
| Carrier to CarrierCalendar | One carrier to many daily operating records | Cascade delete; unique carrier/date |
| Facility to FacilityCalendar | One facility to many daily operating records | Cascade delete; unique facility/date |
| Shipment to AnomalyAudit | One shipment may have multiple audit issues | Cascade delete; one row per shipment/anomaly type |
| AnomalyAudit to anomaly evidence | One case to its typed evidence row(s) | Evidence tables have audit and source-record foreign keys |

`AnomalyAudit` stores a typed `target_entity_type` and `target_record_id`. The validator resolves that pair against the corresponding table and key; there is no unsafe generic foreign key. Evidence tables reference source records with ordinary foreign keys: route-sequence evidence references legs, state observations reference the shipment and delivered event, and ETA leakage evidence references an ETA and later event. These records represent intentional anomalies without mutating operational rows. `ground_truth.csv` contains delivery labels, anomaly IDs, and evidence IDs; it is not a model input table.

## Indexes

Indexes support shipment status/due date, route lookup, ordered legs, event time and ingestion time, facility queue history, weather validity windows, ETA history, and delay-reason history. Primary keys and declared unique constraints provide their own indexes.
