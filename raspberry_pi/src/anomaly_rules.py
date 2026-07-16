"""
anomaly_rules.py

Rule-based anomaly detection. Runs on every validated payload.

Checks (in order):
1. Out-of-range values
2. Rapid change from the previous valid reading
3. Stuck sensor (same value repeated for too many consecutive readings)
4. Device fault flag
5. Cross-check between device01 and device02 (indoor nodes)
6. Missing data timeout (call check_missing_data() on a scheduler)

Returns a dict:
  {
    "rule_status": "normal" | "anomaly",
    "rule_flags":  [ "TEMPERATURE_OUT_OF_RANGE", ... ]
  }
"""

import logging
import time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH        = Path(__file__).parent.parent / "config" / "rule_config.yaml"
MODBUS_CONFIG_PATH = Path(__file__).parent.parent / "config" / "modbus_config.yaml"

def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

_cfg        = _load_yaml(CONFIG_PATH)
_modbus_cfg = _load_yaml(MODBUS_CONFIG_PATH)

RANGES        = _cfg["ranges"]
RAPID_CHANGE  = _cfg["rapid_change"]
CROSS_CHECK   = _cfg["indoor_cross_check"]
STUCK_COUNT   = _cfg["stuck_sensor"]["consecutive_count"]

# Missing-data timeout is expressed as a multiple of the poll interval
# (modbus_config.yaml's poll_interval_seconds) rather than a fixed number
# of seconds, so it scales automatically whether the collector is polling
# every 10s (dev/test) or every 600s (production) -- a fixed absolute
# timeout tuned for one would either never trigger or constantly false-
# positive under the other.
MISSING_TIMEOUT_MULTIPLIER = _cfg["missing_data"]["timeout_multiplier"]
MISSING_TIMEOUT = _modbus_cfg["poll_interval_seconds"] * MISSING_TIMEOUT_MULTIPLIER

# ── Fields checked per device ─────────────────────────────────────────────────
RANGE_FIELDS = {
    "device01": ["temperature", "humidity", "co2"],
    "device02": ["temperature", "humidity", "co2"],
    "device03": ["temperature", "humidity", "wind_speed", "wind_direction",
                 "rainfall", "solar_radiation", "pressure"],
}

# wind_direction is deliberately excluded here -- see rule_config.yaml's
# comment on rapid_change: plain subtraction breaks at the 0/360 wraparound.
RAPID_FIELDS = {
    "device01": ["temperature", "humidity", "co2"],
    "device02": ["temperature", "humidity", "co2"],
    "device03": ["temperature", "humidity", "wind_speed", "rainfall",
                 "solar_radiation", "pressure"],
}

# Deliberately narrower than RANGE_FIELDS/RAPID_FIELDS -- see
# rule_config.yaml's comment on stuck_sensor for why rainfall/wind_speed/
# solar_radiation/pressure are excluded (long stretches at the same value,
# e.g. 0, are normal for those, not a fault signal).
STUCK_FIELDS = {
    "device01": ["temperature", "humidity", "co2"],
    "device02": ["temperature", "humidity", "co2"],
    "device03": ["temperature", "humidity"],
}

INDOOR_DEVICES = ("device01", "device02")

# ── In-memory state (per site + device) ───────────────────────────────────────
# key: (site_id, device_id)
_last_values: dict[tuple, dict] = {}   # previous reading (any outcome, faulted or not)
_last_seen:   dict[tuple, float] = {}  # epoch time of last *successful* reading (absent if never successful)
_indoor_latest: dict[str, dict] = {}   # site_id → {device_id → payload}
_stuck_state: dict[tuple, dict[str, tuple]] = {}  # key -> field -> (last_value, streak_count)
_known_devices: set[tuple] = set()     # every (site_id, device_id) ever seen, success or failure

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

def _check_rapid_change(payload: dict, device_id: str, key: tuple, flags: list) -> None:
    prev = _last_values.get(key)
    if prev is None:
        return
    for field in RAPID_FIELDS.get(device_id, []):
        curr_val = payload.get(field)
        prev_val = prev.get(field)
        if curr_val is None or prev_val is None:
            continue
        threshold = RAPID_CHANGE.get(field)
        if threshold and abs(curr_val - prev_val) > threshold:
            flags.append(_flag(field, "RAPID_CHANGE"))

def _check_stuck_sensor(payload: dict, device_id: str, key: tuple, flags: list) -> None:
    state = _stuck_state.setdefault(key, {})
    for field in STUCK_FIELDS.get(device_id, []):
        value = payload.get(field)
        if value is None:
            continue  # a failed/omitted reading doesn't count as a repeat -- just skip it

        last_value, streak = state.get(field, (None, 0))
        streak = streak + 1 if last_value is not None and value == last_value else 1
        state[field] = (value, streak)

        if streak >= STUCK_COUNT:
            flags.append(_flag(field, "STUCK_SENSOR"))

def _check_indoor_cross(site_id: str, flags: list) -> None:
    site_data = _indoor_latest.get(site_id, {})
    d1 = site_data.get("device01")
    d2 = site_data.get("device02")
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
    if payload.get("device_fault") == "true":
        flags.append("DEVICE_FAULT")

def _update_state(payload: dict, key: tuple) -> None:
    _last_values[key] = payload
    _known_devices.add(key)

    # last_seen (used by the missing-data watchdog) only advances on a
    # *successful* reading, not merely a received payload -- a device that
    # is continuously Modbus-faulting still produces a payload every poll
    # (device_fault="true"), and treating that as "present" would mean
    # MISSING_DATA could never fire for a device that's been broken for
    # hours, since DEVICE_FAULT alone only reflects the current cycle.
    # _known_devices (above) is updated unconditionally instead, so a
    # device that has *never once* succeeded is still checked below --
    # see check_missing_data().
    if payload.get("device_fault") != "true":
        _last_seen[key] = time.time()

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
    _check_rapid_change(payload, device_id, key, flags)
    _check_stuck_sensor(payload, device_id, key, flags)
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
    Check all known devices for missing *valid* data (see _update_state).
    Call this on a periodic scheduler (e.g. every 30s).

    Returns a dict of {(site_id, device_id): ["MISSING_DATA"]}
    for any device that hasn't produced a successful reading recently
    enough -- including a device that has been polled but has *never*
    produced one, which has no entry in _last_seen at all (treated the
    same as "last successful reading was infinitely long ago").
    """
    now     = time.time()
    missing = {}
    for key in _known_devices:
        last = _last_seen.get(key)
        if last is None or now - last > MISSING_TIMEOUT:
            logger.warning("[%s / %s] Missing data timeout", *key)
            missing[key] = ["MISSING_DATA"]
    return missing
