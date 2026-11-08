# Point-in-time ETA features

Build the starter bundle from the repository root:

```powershell
python -m src.features.build_features --db-path data/starter/logitrack.sqlite --output-dir data/starter/features
```

The builder emits `features.csv`, `targets.csv`, `feature_dictionary.csv`, and `validation_summary.json`. There is one feature and target row per `ETAEstimate` snapshot. `shipment_id`, `eta_id`, and `predicted_at` are carried as identifiers/metadata; exclude all three from the model matrix.

## As-of availability

`predicted_at` is the point-in-time cutoff. A tracking scan is eligible only when both `event_time <= predicted_at` and `ingestion_time <= predicted_at`. Weather observations require `observed_at <= predicted_at` and are counted as active only while their validity interval covers the prediction time. Queue observations require `observed_at <= predicted_at`. Carrier and service-level reference records are joined only when their effective dates cover the prediction time. Planned route fields are taken from the shipment's assigned legs. Current `Shipment.lifecycle_state`, `ShipmentLeg.leg_state`, actual pickup/delivery fields, `ETAEstimate.estimated_delivery_time`, confidence, model version, delay reasons, ground truth, audit rows, and anomaly evidence are not read into predictors.

Point-in-time state is inferred from the latest eligible tracking event. Missing measurements remain empty in CSV and have explicit indicator features where absence affects interpretation (`weight_missing`, `latest_event_missing`, `pickup_scan_seen`, `weather_observation_missing`, `queue_wait_missing`, and `queue_observation_missing`). Queue aggregate values remain null when no eligible queue observation exists; mean wait is also null with `queue_wait_missing=1` when queue data exists without a wait estimate. Weather exposure counts and max severity are zero when no active observed disruption is present; a separate missing flag distinguishes no weather observations from no active exposure.

## Target definition and separation

`targets.csv` is separate from `features.csv`. `target_delivery_time` is `Shipment.actual_delivery_time`, and `target_remaining_hours` is `(actual_delivery_time - predicted_at)` in hours. It is populated only when the actual delivery exists and occurs after the prediction timestamp; otherwise the target duration is null and `target_eligible=0`. Actual delivery is queried only by the target builder and is never selected or used by the predictor builder.

`feature_dictionary.csv` documents each identifier, predictor, and target, including data type, source, definition, and missing-value policy. The validation summary reports row counts, field types, missing counts, source allowlists, and the event cutoff audit. On the existing starter data, the expected cutoff audit is 600 event/snapshot pairs: 289 available, 18 occurred by prediction but ingested late, and 293 occurred after prediction.
