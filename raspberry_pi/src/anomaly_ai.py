"""
anomaly_ai.py

AI-based anomaly scoring using Isolation Forest (scikit-learn).
Runs per device, unsupervised — no labeled data required.

One model is maintained per (site_id, device_id) pair.
Models are trained once enough samples have been collected,
and retrained periodically as new data arrives.

Returns a dict:
  {
    "ai_score":  0.12,         # 0.0 (normal) ~ 1.0 (anomaly)
    "ai_status": "normal"      # "normal" | "anomaly" | "pending"
  }
"""

import logging
import pickle
from pathlib import Path

import numpy as np
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_DIR        = Path(__file__).parent.parent / "models"
MIN_TRAIN_SAMPLES = 100     # minimum samples before training
RETRAIN_EVERY     = 500     # retrain after this many new samples
ANOMALY_THRESHOLD = 0.5     # ai_score >= this → "anomaly"
CONTAMINATION     = 0.05    # expected anomaly ratio for IsolationForest

MODEL_DIR.mkdir(exist_ok=True)

# ── Fields used for scoring per device ───────────────────────────────────────
SCORE_FIELDS = {
    "indoor_01":  ["temperature", "humidity", "co2"],
    "indoor_02":  ["temperature", "humidity", "co2"],
    "outdoor_01": ["temperature", "humidity", "wind_speed", "solar_radiation"],
}

# ── In-memory state per (site_id, device_id) ─────────────────────────────────
_buffers: dict[tuple, list[list[float]]] = {}   # raw sample buffer
_models:  dict[tuple, IsolationForest]   = {}   # trained models
_counts:  dict[tuple, int]               = {}   # samples seen since last train

# ── Persistence ───────────────────────────────────────────────────────────────
def _model_path(site_id: str, device_id: str) -> Path:
    return MODEL_DIR / f"{site_id}_{device_id}.pkl"

def _save_model(key: tuple, model: IsolationForest) -> None:
    site_id, device_id = key
    with open(_model_path(site_id, device_id), "wb") as f:
        pickle.dump(model, f)
    logger.info("[%s / %s] Model saved.", site_id, device_id)

def _load_model(key: tuple) -> IsolationForest | None:
    site_id, device_id = key
    path = _model_path(site_id, device_id)
    if not path.exists():
        return None
    with open(path, "rb") as f:
        model = pickle.load(f)
    logger.info("[%s / %s] Model loaded from disk.", site_id, device_id)
    return model

# ── Helpers ───────────────────────────────────────────────────────────────────
def _extract_features(payload: dict, device_id: str) -> list[float] | None:
    fields = SCORE_FIELDS.get(device_id)
    if fields is None:
        return None
    values = []
    for field in fields:
        v = payload.get(field)
        if v is None or not isinstance(v, (int, float)):
            return None
        values.append(float(v))
    return values

def _train(key: tuple) -> IsolationForest | None:
    site_id, device_id = key
    samples = _buffers.get(key, [])
    if len(samples) < MIN_TRAIN_SAMPLES:
        return None
    X = np.array(samples)
    model = IsolationForest(contamination=CONTAMINATION, random_state=42)
    model.fit(X)
    _save_model(key, model)
    logger.info("[%s / %s] Model trained on %d samples.", site_id, device_id, len(samples))
    return model

def _normalize_score(raw: float) -> float:
    """
    IsolationForest.decision_function returns:
      positive → more normal, negative → more anomalous.
    Normalize to 0.0 (normal) ~ 1.0 (anomaly).
    """
    # Clamp to [-0.5, 0.5] then invert and rescale to [0, 1]
    clamped = max(-0.5, min(0.5, raw))
    return round(0.5 - clamped, 4)

# ── Public API ────────────────────────────────────────────────────────────────
def score(payload: dict) -> dict:
    """
    Score a validated payload for anomalies.
    Updates the sample buffer and retrains the model when thresholds are met.

    Returns {"ai_score": float | null, "ai_status": str}.
    """
    site_id   = payload["site_id"]
    device_id = payload["device_id"]
    key       = (site_id, device_id)

    features = _extract_features(payload, device_id)
    if features is None:
        return {"ai_score": None, "ai_status": "pending"}

    # Buffer the sample
    _buffers.setdefault(key, []).append(features)
    _counts[key] = _counts.get(key, 0) + 1

    # Load model from disk on first encounter
    if key not in _models:
        loaded = _load_model(key)
        if loaded:
            _models[key] = loaded

    # Train or retrain
    if key not in _models or _counts[key] >= RETRAIN_EVERY:
        model = _train(key)
        if model:
            _models[key] = model
            _counts[key] = 0

    # Score
    model = _models.get(key)
    if model is None:
        return {"ai_score": None, "ai_status": "pending"}

    X         = np.array([features])
    raw_score = model.decision_function(X)[0]
    ai_score  = _normalize_score(raw_score)
    ai_status = "anomaly" if ai_score >= ANOMALY_THRESHOLD else "normal"

    return {"ai_score": ai_score, "ai_status": ai_status}
