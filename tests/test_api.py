import hashlib
import json
import asyncio
from pathlib import Path

import joblib
import httpx
import numpy as np
import pandas as pd

from src.api.app import create_app


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "realistic_v2"
FEATURES = ROOT / "data" / "realistic_v2" / "features" / "features.csv"
CONFIG = json.loads((MODEL_DIR / "preprocessing_config.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_payload() -> dict:
    row = pd.read_csv(FEATURES, nrows=1).iloc[0]
    result = {}
    for key, value in row.items():
        if key not in CONFIG["predictor_columns"]:
            continue
        result[key] = None if pd.isna(value) else value.item() if hasattr(value, "item") else value
    return result


def _requests(app, requests):
    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                results = []
                for method, path, body in requests:
                    results.append(await client.request(method, path, json=body))
                return results
    return asyncio.run(run())


def test_health_model_info_and_successful_prediction_use_saved_pipeline(monkeypatch):
    from src.prediction import train_realistic_v2

    def no_training(*args, **kwargs):
        raise AssertionError("API must not train a model")

    monkeypatch.setattr(train_realistic_v2, "run_training", no_training)
    before = {str(path.relative_to(MODEL_DIR)): _sha256(path)
              for path in MODEL_DIR.rglob("*") if path.is_file()}
    expected = float(joblib.load(MODEL_DIR / "ridge_pipeline.joblib").predict(
        pd.DataFrame([_valid_payload()], columns=CONFIG["predictor_columns"]))[0])
    responses = _requests(create_app(MODEL_DIR), [
        ("GET", "/", None), ("GET", "/health", None), ("GET", "/model-info", None),
        ("GET", "/dashboard", None), ("GET", "/assets/dashboard.css", None),
        ("GET", "/dashboard/metadata", None), ("GET", "/dashboard/demo", None),
        ("POST", "/predict", _valid_payload()),
    ])
    assert responses[0].json()["name"] == "LogiTrack AI ETA API"
    assert responses[1].json() == {"status": "ok", "model_loaded": True}
    info = responses[2].json()
    assert info["model_name"] == "Ridge Regression"
    assert info["expected_predictor_count"] == 40
    assert info["training_dataset"] == "data/realistic_v2"
    assert responses[3].status_code == 200 and "Logistics Intelligence Dashboard" in responses[3].text
    assert responses[4].status_code == 200 and "--navy" in responses[4].text
    metadata = responses[5].json()
    assert metadata["predictor_count"] == 40
    assert set(metadata["predictor_columns"]) == set(CONFIG["predictor_columns"])
    assert not set(metadata["predictor_columns"]) & {"target_remaining_hours", "actual_delivery_time", "ground_truth", "anomaly_type"}
    demo = responses[6].json()
    assert demo["source"] == "realistic_v2 synthetic feature sample"
    assert set(demo["features"]) == set(CONFIG["predictor_columns"])
    response = responses[7]
    assert response.status_code == 200
    body = response.json()
    assert body["target"] == "target_remaining_hours"
    assert body["units"] == "hours"
    assert isinstance(body["predicted_remaining_hours"], (int, float))
    assert np.isfinite(body["predicted_remaining_hours"])
    assert body["predicted_remaining_hours"] == expected
    after = {str(path.relative_to(MODEL_DIR)): _sha256(path)
             for path in MODEL_DIR.rglob("*") if path.is_file()}
    assert before == after


def test_predict_rejects_missing_fields_and_invalid_types():
    payload = _valid_payload()
    payload.pop("service_mode")
    invalid = _valid_payload()
    invalid["service_target_transit_hours"] = "quick"
    responses = _requests(create_app(MODEL_DIR), [
        ("POST", "/predict", payload), ("POST", "/predict", invalid),
    ])
    assert responses[0].status_code == 422
    assert "service_mode" in responses[0].json()["detail"]["missing_fields"]
    assert responses[1].status_code == 422
    assert "service_target_transit_hours" in responses[1].json()["detail"]["invalid_fields"]


def test_predict_rejects_targets_outcomes_and_anomaly_annotations():
    requests = []
    fields = ("actual_delivery_time", "target_remaining_hours", "ground_truth", "anomaly_type")
    for field in fields:
        payload = _valid_payload()
        payload[field] = "forbidden"
        requests.append(("POST", "/predict", payload))
    responses = _requests(create_app(MODEL_DIR), requests)
    for field, response in zip(fields, responses):
        assert response.status_code == 422
        assert field in response.json()["detail"]["unexpected_fields"]
