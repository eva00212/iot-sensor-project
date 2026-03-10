"""
anomaly_rules.py

Rule-based anomaly detection. Runs on every validated payload.

Checks (in order):
1. Out-of-range values
2. Sudden change from previous reading
3. Cross-check between indoor_01 and indoor_02
4. Device fault flag
5. Missing data timeout (call check_missing_data() on a scheduler)

Returns a dict:
  {
    "rule_status": "normal" | "anomaly",
    "rule_flags":  [ "TEMP_OUT_OF_RANGE", ... ]
  }
"""

import logging
import time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "rule_config.yaml"

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

_cfg = _load_config()

RANGES         = _cfg["ranges"]
SUDDEN_CHANGE  = _cfg["sudden_change"]
CROSS_CHECK    = _cfg["indoor_cross_check"]
MISSING_TIMEOUT = _cfg["missing_data"]["timeout_seconds"]

# ── Fields checked per device ─────────────────────────────────────────────────
RANGE_FIELDS = {
    "indoor_01":  ["temperature", "humidity", "co2"],
    "indoor_02":  ["temperature", "humidity", "co2"],
    "outdoor_01": ["temperature", "humidity", "wind_speed", "solar_radiation"],
}

SUDDEN_FIELDS = {
    "indoor_01":  ["temperature", "humidity", "co2"],
    "indoor_02":  ["temperature", "humidity", "co2"],
    "outdoor_01": ["temperature", "humidity", "wind_speed", "solar_radiation"],
}

INDOOR_DEVICES = ("indoor_01", "indoor_02")

# ── In-memory state (per site+device) ─────────────────────────────────────────
# key: (site_id, device_id)
_last_values: dict[tuple, dict] = {}   # previous sensor readings
_last_seen:   dict[tuple, float] = {}  # epoch time of last message
_indoor_latest: dict[str, dict] = {}   # site_id → {device_id → payload}

# ── Helpers ───────────────────────────────────────────────────────────────────
def _flag(field: str, suffix: str) -> str:
    return f"{field.upper()}_{suffix}"

def _check_ranges(payload: dict, device_id: str, flags: list) -> None:
    for field in RANGE_FIELDS.get(device_id, []):
        value = payload.get(field)
        if value is None:
            continue
        rule = RANGES.get(field, {})
        lo   = rule.get("min")
        hi   = rule.get("max")
        if lo is not None and value < lo:
            flags.append(_flag(field, "OUT_OF_RANGE"))
        elif hi is not None and value > hi:
            flags.append(_flag(field, "OUT_OF_RANGE"))

def _check_sudden_change(payload: dict, device_id: str, key: tuple, flags: list) -> None:
    prev = _last_values.get(key)
    if prev is None:
        return
    for field in SUDDEN_FIELDS.get(device_id, []):
        curr_val = payload.get(field)
        prev_val = prev.get(field)
        if curr_val is None or prev_val is None:
            continue
        threshold = SUDDEN_CHANGE.get(field)
        if threshold and abs(curr_val - prev_val) > threshold:
            flags.append(_flag(field, "SUDDEN_CHANGE"))

def _check_indoor_cross(site_id: str, flags: list) -> None:
    site_data = _indoor_latest.get(site_id, {})
    d1 = site_data.get("indoor_01")
    d2 = site_data.get("indoor_02")
    if d1 is None or d2 is None:
        return
    for field, threshold in CROSS_CHECK.items():
        v1 = d1.get(field)
        v2 = d2.get(field)
        if v1 is None or v2 is None:
            continue
        if abs(v1 - v2) > threshold:
            flags.append(_flag(field, "INDOOR_MISMATCH"))

def _check_device_fault(payload: dict, flags: list) -> None:
    if payload.get("device_fault") is True:
        flags.append("DEVICE_FAULT")

def _update_state(payload: dict, key: tuple) -> None:
    _last_values[key] = payload
    _last_seen[key]   = time.time()

    site_id   = payload["site_id"]
    device_id = payload["device_id"]
    if device_id in INDOOR_DEVICES:
        _indoor_latest.setdefault(site_id, {})[device_id] = payload

# ── Public API ────────────────────────────────────────────────────────────────
def check(payload: dict) -> dict:
    """
    Run all rule-based checks on a validated payload.
    Returns {"rule_status": ..., "rule_flags": [...]}.
    """
    site_id   = payload["site_id"]
    device_id = payload["device_id"]
    key       = (site_id, device_id)
    flags: list[str] = []

    _check_ranges(payload, device_id, flags)
    _check_sudden_change(payload, device_id, key, flags)
    _check_device_fault(payload, flags)

    # Update state before cross-check so the current payload is included
    _update_state(payload, key)

    if device_id in INDOOR_DEVICES:
        _check_indoor_cross(site_id, flags)

    # Deduplicate while preserving order
    seen = set()
    unique_flags = [f for f in flags if not (f in seen or seen.add(f))]

    return {
        "rule_status": "anomaly" if unique_flags else "normal",
        "rule_flags":  unique_flags,
    }


def check_missing_data() -> dict[tuple, list]:
    """
    Check all known devices for missing data timeout.
    Call this on a periodic scheduler (e.g. every 30s).

    Returns a dict of {(site_id, device_id): ["MISSING_DATA"]}
    for any device that has gone silent.
    """
    now     = time.time()
    missing = {}
    for key, last in _last_seen.items():
        if now - last > MISSING_TIMEOUT:
            logger.warning("[%s / %s] Missing data timeout", *key)
            missing[key] = ["MISSING_DATA"]
    return missing
