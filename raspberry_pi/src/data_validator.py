"""
data_validator.py

Validates raw MQTT payloads before passing them to anomaly detection.

Checks:
- Required fields present
- Correct data types
- Timestamp format (ISO 8601)
- No future timestamps
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── Required fields per device type ──────────────────────────────────────────
# Only fields that are *always* present, success or failure, are required.
# temperature/humidity/co2/wind_speed/etc. are all omitted (not zeroed)
# when a Modbus read fails, so none of them can be required here -- see
# COMMON_OPTIONAL_FIELDS/DEVICE_OPTIONAL_FIELDS below.
COMMON_FIELDS = {
    "site_id":      str,
    "device_id":    str,
    "timestamp":    str,
    "device_fault": str,
}

DEVICE_EXTRA_FIELDS = {
    "device01": {},
    "device02": {},
    "device03": {},
}

# Optional fields validated only when present in the payload, regardless
# of device_id -- e.g. error_message/retry_count, set by modbus_poller
# when a Modbus read exhausts all retries (alongside device_fault = "true").
# temperature/humidity are here (not in COMMON_FIELDS) because every
# device type has them, but only when the read succeeded.
COMMON_OPTIONAL_FIELDS = {
    "temperature":   (int, float),
    "humidity":      (int, float),
    "error_message": str,
    "retry_count":   int,
}

# Optional fields: validated only when present in the payload
DEVICE_OPTIONAL_FIELDS = {
    "device01": {"co2": (int, float)},
    "device02": {"co2": (int, float)},
    "device03": {
        "wind_speed":     (int, float),
        "wind_direction": (int, float),
        "rainfall":       (int, float),
        "rain_detected":  str,
        "solar_radiation": (int, float),
        "pressure":       (int, float),
    },
}

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

# ── Exceptions ────────────────────────────────────────────────────────────────
class ValidationError(Exception):
    pass

# ── Helpers ───────────────────────────────────────────────────────────────────
def _check_fields(payload: dict, fields: dict) -> None:
    for field, expected_type in fields.items():
        if field not in payload:
            raise ValidationError(f"Missing required field: '{field}'")
        if not isinstance(payload[field], expected_type):
            raise ValidationError(
                f"Field '{field}' has wrong type: "
                f"expected {expected_type}, got {type(payload[field]).__name__}"
            )

def _check_optional_fields(payload: dict, fields: dict) -> None:
    for field, expected_type in fields.items():
        if field not in payload:
            continue
        if not isinstance(payload[field], expected_type):
            raise ValidationError(
                f"Field '{field}' has wrong type: "
                f"expected {expected_type}, got {type(payload[field]).__name__}"
            )

def _check_timestamp(ts: str) -> None:
    try:
        dt = datetime.strptime(ts, TIMESTAMP_FORMAT)
    except ValueError:
        raise ValidationError(
            f"Invalid timestamp format: '{ts}'. Expected '{TIMESTAMP_FORMAT}'"
        )
    now = datetime.now()
    if dt > now + timedelta(seconds=60):
        raise ValidationError(f"Timestamp is in the future: '{ts}'")

# ── Public API ────────────────────────────────────────────────────────────────
def validate(payload: dict) -> dict:
    """
    Validate a raw sensor payload.

    Returns the payload unchanged if valid.
    Raises ValidationError describing the first problem found.
    """
    # Common fields
    _check_fields(payload, COMMON_FIELDS)

    # Timestamp format and sanity
    _check_timestamp(payload["timestamp"])

    # Device-specific fields
    device_id = payload["device_id"]
    extra = DEVICE_EXTRA_FIELDS.get(device_id)
    if extra is None:
        raise ValidationError(f"Unknown device_id: '{device_id}'")
    _check_fields(payload, extra)

    optional = {**COMMON_OPTIONAL_FIELDS, **DEVICE_OPTIONAL_FIELDS.get(device_id, {})}
    _check_optional_fields(payload, optional)

    return payload


def safe_validate(payload: dict) -> dict | None:
    """
    Wrapper around validate() that logs and returns None on failure
    instead of raising, for use in the pipeline.
    """
    try:
        return validate(payload)
    except ValidationError as e:
        site_id   = payload.get("site_id", "?")
        device_id = payload.get("device_id", "?")
        logger.warning("[%s / %s] Validation failed: %s", site_id, device_id, e)
        return None
