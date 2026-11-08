# LogiTrack AI ETA API

This is a small inference-only FastAPI service for the existing Ridge pipeline in `models/realistic_v2/ridge_pipeline.joblib`. It does not train a model or change the dataset. The model expects the 40 point-in-time predictors recorded in `models/realistic_v2/preprocessing_config.json`.

## Install

From the project root, install the project dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Start the server

```powershell
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

The app loads `models/realistic_v2/ridge_pipeline.joblib` once at startup. To use another model directory, set `LOGITRACK_MODEL_DIR` before starting the process; that directory must contain the same model files and configuration format.

Open the responsive dashboard at `http://127.0.0.1:8000/dashboard`. It uses the same API process and model. Its dropdown values come from the fitted one-hot encoder, and its demo button loads an actual predictor row from `data/realistic_v2/features/features.csv`. The dashboard sends predictions to `POST /predict`; it does not run inference in JavaScript.

## Endpoints

- `GET /` returns the API name and version.
- `GET /health` reports whether the model loaded. A load failure returns `degraded` and exposes an error message.
- `GET /model-info` returns the selected model, target, 40 expected predictors, dataset name, and training period/row count.
- `POST /predict` accepts one flat JSON object containing all 40 predictor fields. Missing fields, unknown fields, targets, actual outcomes, ground-truth fields, and anomaly annotations are rejected with HTTP 422. Numeric and categorical source fields may be `null` where the saved feature builder allows imputation; boolean indicator fields must be `true`/`false` or integer `0`/`1`. Unknown category strings are accepted because the fitted one-hot encoder ignores unseen categories.

The service uses the fitted Ridge pipeline directly. It does not read delivery outcomes or recompute point-in-time features; callers must provide values constructed using the documented cutoff rules. In particular, event features must observe both `event_time <= predicted_at` and `ingestion_time <= predicted_at`.

## Example request

```json
{
  "service_mode": "parcel",
  "service_target_transit_hours": 48.0,
  "service_reference_missing": 0,
  "carrier_type": "parcel",
  "carrier_reference_missing": 0,
  "carrier_historical_on_time_rate": 0.82,
  "carrier_historical_delivery_count": 12,
  "carrier_history_missing": 0,
  "shipment_priority": "standard",
  "shipment_weight_kg": 18.5,
  "weight_missing": 0,
  "journey_elapsed_minutes": 420.0,
  "planned_route_distance_km": 680.0,
  "planned_route_hours": 12.0,
  "planned_leg_count": 2,
  "remaining_scheduled_leg_count": 1,
  "remaining_scheduled_hours": 7.0,
  "remaining_scheduled_distance_km": 390.0,
  "eligible_event_count": 3,
  "latest_event_type": "departed",
  "latest_event_missing": 0,
  "last_observed_state": "in_transit",
  "latest_event_age_minutes": 25.0,
  "latest_event_leg_number": 1,
  "pickup_scan_seen": 1,
  "minutes_since_pickup_scan": 390.0,
  "weather_observation_count": 0,
  "active_weather_disruption_count": 0,
  "max_active_weather_severity": 0,
  "forecast_weather_count": 1,
  "max_forecast_weather_severity": 2,
  "forecast_precipitation_mm": 3.5,
  "forecast_wind_speed_kph": 18.0,
  "forecast_weather_missing": 0,
  "weather_observation_missing": 1,
  "queue_observation_count": 1,
  "max_observed_queue_length": 22,
  "mean_observed_wait_minutes": 35.0,
  "queue_wait_missing": 0,
  "queue_observation_missing": 0
}
```

## Example response

```json
{
  "model_name": "Ridge Regression",
  "target": "target_remaining_hours",
  "predicted_remaining_hours": 8.42,
  "units": "hours"
}
```

The numeric response above is illustrative. Actual responses come from the saved model and submitted feature values.

## Swagger UI

Start the server, then open `http://127.0.0.1:8000/docs`. Expand `POST /predict`, choose **Try it out**, paste the full 40-field request body, and select **Execute**. The interactive docs also list the health and model-info endpoints.

The dashboard uses two additional read-only endpoints: `GET /dashboard/metadata` supplies the predictor types, model categories, and dataset metrics; `GET /dashboard/demo` supplies the real synthetic feature sample. Neither endpoint returns targets or anomaly labels.

## Run API tests

```powershell
python -m pytest tests/test_api.py -q
```

This model was trained on synthetic data. The API is an inference demonstration, not evidence of real-world accuracy or production readiness.
