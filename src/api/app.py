"""Small inference-only FastAPI service for the saved realistic_v2 Ridge model."""
from __future__ import annotations

import json
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DIR = PROJECT_ROOT / "models" / "realistic_v2"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
API_NAME = "LogiTrack AI ETA API"


def _load_metadata(model_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = json.loads((model_dir / "preprocessing_config.json").read_text(encoding="utf-8"))
    card = json.loads((model_dir / "model_card.json").read_text(encoding="utf-8"))
    split_path = PROJECT_ROOT / "reports" / "realistic_v2" / "split_summary.json"
    split = json.loads(split_path.read_text(encoding="utf-8")) if split_path.exists() else {}
    dictionary_path = PROJECT_ROOT / config["source_dataset"] / "features" / "feature_dictionary.csv"
    dictionary = pd.read_csv(dictionary_path)
    predictor_rows = dictionary[dictionary.role == "predictor"]
    predictor_types = dict(zip(predictor_rows.name.astype(str), predictor_rows.data_type.astype(str)))
    if set(predictor_types) != set(config.get("predictor_columns", [])):
        raise ValueError("feature dictionary fields differ from the saved model contract")
    config["predictor_types"] = predictor_types
    return config, card, split


def _validate_payload(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    predictors = list(config["predictor_columns"])
    given = set(payload)
    required = set(predictors)
    missing = sorted(required - given)
    extra = sorted(given - required)
    if missing or extra:
        detail: dict[str, Any] = {}
        if missing:
            detail["missing_fields"] = missing
        if extra:
            detail["unexpected_fields"] = extra
        raise HTTPException(status_code=422, detail=detail)

    predictor_types = config.get("predictor_types", {})
    bool_fields = set(config["boolean_columns_treated_as_numeric_0_1"])
    numeric_fields = set(config["numeric_columns"])
    categorical_fields = set(config["categorical_columns"])
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name in predictors:
        value = payload[name]
        if name in bool_fields:
            if isinstance(value, bool):
                clean[name] = int(value)
            elif isinstance(value, int) and value in (0, 1):
                clean[name] = value
            else:
                errors[name] = "must be a boolean or integer 0/1"
        elif name in categorical_fields:
            if value is None or isinstance(value, str):
                clean[name] = value
            else:
                errors[name] = "must be a string or null"
        elif name in numeric_fields:
            declared = predictor_types.get(name, "float")
            if value is None:
                clean[name] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                errors[name] = "must be a finite number or null"
            elif declared == "integer" and int(value) != value:
                errors[name] = "must be an integer or null"
            else:
                clean[name] = int(value) if declared == "integer" else float(value)
        else:
            errors[name] = "has no supported type in the saved model contract"
    if errors:
        raise HTTPException(status_code=422, detail={"invalid_fields": errors})
    return clean


def create_app(model_dir: Path | str | None = None) -> FastAPI:
    """Create the API app; model is loaded once during startup and never trained here."""
    artifact_dir = Path(model_dir) if model_dir is not None else Path(os.getenv("LOGITRACK_MODEL_DIR", DEFAULT_MODEL_DIR))
    if not artifact_dir.is_absolute():
        artifact_dir = PROJECT_ROOT / artifact_dir

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.model = None
        app.state.model_loaded = False
        app.state.load_error = None
        try:
            config, card, split = _load_metadata(artifact_dir)
            model_path = artifact_dir / "ridge_pipeline.joblib"
            if card.get("selected_model") != "ridge" or config.get("target") != "target_remaining_hours":
                raise ValueError("saved artifact metadata does not describe the selected ETA Ridge model")
            if config.get("predictor_count") != len(config.get("predictor_columns", [])):
                raise ValueError("saved predictor count does not match predictor list")
            checks = config.get("point_in_time_checks", {})
            if checks.get("event_cutoff_applied_twice") is not True or checks.get("target_fields_absent_from_predictors") is not True:
                raise ValueError("saved artifact metadata does not confirm point-in-time/leakage checks")
            app.state.model = joblib.load(model_path)
            app.state.model_loaded = True
            app.state.model_path = model_path
            app.state.model_config = config
            app.state.model_card = card
            app.state.split_summary = split
            validation_path = PROJECT_ROOT / config["source_dataset"] / "features" / "validation_summary.json"
            app.state.feature_validation = json.loads(validation_path.read_text(encoding="utf-8"))
            categorical = app.state.model.named_steps["preprocessing"].named_transformers_["categorical"].named_steps["onehot"]
            app.state.categories = {
                name: [str(value) for value in values]
                for name, values in zip(config["categorical_columns"], categorical.categories_)
            }
            try:
                feature_path = PROJECT_ROOT / config["source_dataset"] / "features" / "features.csv"
                demo = pd.read_csv(feature_path, usecols=config["predictor_columns"], nrows=1).iloc[0].to_dict()
                app.state.demo_features = {
                    key: None if pd.isna(value) else value.item() if hasattr(value, "item") else value
                    for key, value in demo.items()
                }
            except Exception:
                app.state.demo_features = None
        except Exception as exc:  # Health remains reachable so operators can see load failures.
            app.state.load_error = str(exc)
        yield

    app = FastAPI(title=API_NAME, version="1.0.0", lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=DASHBOARD_DIR / "assets"), name="dashboard-assets")

    def ensure_model() -> None:
        if not getattr(app.state, "model_loaded", False):
            raise HTTPException(status_code=503, detail="ETA model is not loaded")

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"name": API_NAME, "version": app.version, "description": "Inference-only ETA regression service"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(DASHBOARD_DIR / "index.html", media_type="text/html")

    @app.get("/dashboard/metadata", include_in_schema=False)
    def dashboard_metadata() -> dict[str, Any]:
        ensure_model()
        config = app.state.model_config
        return {
            "model_name": "Ridge Regression",
            "target": config["target"],
            "predictor_count": config["predictor_count"],
            "predictor_columns": config["predictor_columns"],
            "predictor_types": config["predictor_types"],
            "boolean_columns": config["boolean_columns_treated_as_numeric_0_1"],
            "numeric_columns": config["numeric_columns"],
            "categorical_columns": config["categorical_columns"],
            "categories": app.state.categories,
            "training_dataset": config["source_dataset"],
            "demo_available": app.state.demo_features is not None,
            "missing_value_counts": app.state.feature_validation.get("missing_value_counts", {}),
            "test_metrics": app.state.model_card.get("test_metrics", {}),
        }

    @app.get("/dashboard/demo", include_in_schema=False)
    def dashboard_demo() -> dict[str, Any]:
        ensure_model()
        if app.state.demo_features is None:
            raise HTTPException(status_code=503, detail="Demo feature sample is unavailable")
        return {"source": "realistic_v2 synthetic feature sample", "features": app.state.demo_features}

    @app.get("/health")
    def health() -> dict[str, Any]:
        loaded = bool(getattr(app.state, "model_loaded", False))
        body: dict[str, Any] = {"status": "ok" if loaded else "degraded", "model_loaded": loaded}
        if not loaded and getattr(app.state, "load_error", None):
            body["load_error"] = app.state.load_error
        return body

    @app.get("/model-info")
    def model_info() -> dict[str, Any]:
        ensure_model()
        config = app.state.model_config
        split = app.state.split_summary
        return {
            "model_name": "Ridge Regression",
            "selected_artifact": app.state.model_path.name,
            "target": config["target"],
            "target_units": "hours",
            "expected_predictor_count": config["predictor_count"],
            "predictor_fields": config["predictor_columns"],
            "training_dataset": config["source_dataset"],
            "training_dataset_type": app.state.model_card.get("dataset"),
            "training_rows": split.get("counts", {}).get("train"),
            "training_period_utc": split.get("date_ranges_utc", {}).get("train"),
        }

    @app.post("/predict")
    def predict(payload: dict[str, Any]) -> dict[str, Any]:
        ensure_model()
        clean = _validate_payload(payload, app.state.model_config)
        order = app.state.model_config["predictor_columns"]
        frame = pd.DataFrame([[clean[name] for name in order]], columns=order)
        try:
            prediction = float(app.state.model.predict(frame)[0])
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"model could not process the supplied feature values: {exc}") from exc
        if not math.isfinite(prediction):
            raise HTTPException(status_code=500, detail="model returned a non-finite prediction")
        return {
            "model_name": "Ridge Regression",
            "target": app.state.model_config["target"],
            "predicted_remaining_hours": prediction,
            "units": "hours",
        }

    return app


app = create_app()
