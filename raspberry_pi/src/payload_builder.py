"""
payload_builder.py

Builds the final payload by combining validated sensor data,
rule-based anomaly results, and AI anomaly score.

Output format (before oneM2M conversion):
{
  "site_id":   "site_01",
  "device_id": "indoor_01",
  "timestamp": "2026-03-10T12:10:21",
  "data": {
    "temperature": 24.6,
    "humidity":    63.2,
    "co2":         512,
    "device_fault": false
  },
  "anomaly": {
    "rule_status": "normal",
    "rule_flags":  [],
    "ai_score":    0.12,
    "ai_status":   "normal"
  }
}

Note: voltage is excluded from the server payload.
      It is used only internally by anomaly_rules.py.
"""

import logging

logger = logging.getLogger(__name__)

# ── Fields included in the server payload per device ─────────────────────────
SERVER_FIELDS = {
    "indoor_01":  ["temperature", "humidity", "co2", "device_fault"],
    "indoor_02":  ["temperature", "humidity", "co2", "device_fault"],
    "outdoor_01": ["temperature", "humidity", "wind_speed", "rain_detected",
                   "solar_radiation", "device_fault"],
}

# ── Public API ────────────────────────────────────────────────────────────────
def build(validated: dict, rule_result: dict, ai_result: dict) -> dict:
    """
    Assemble the final payload from pipeline stage outputs.

    Args:
        validated:   Output of data_validator.validate()
        rule_result: Output of anomaly_rules.check()
        ai_result:   Output of anomaly_ai.score()

    Returns the final payload dict ready for oneM2M conversion.
    """
    site_id   = validated["site_id"]
    device_id = validated["device_id"]
    timestamp = validated["timestamp"]

    fields = SERVER_FIELDS.get(device_id)
    if fields is None:
        logger.error("[%s / %s] Unknown device_id — cannot build payload.", site_id, device_id)
        raise ValueError(f"Unknown device_id: '{device_id}'")

    data = {field: validated[field] for field in fields if field in validated}

    return {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": timestamp,
        "data":      data,
        "anomaly": {
            "rule_status": rule_result["rule_status"],
            "rule_flags":  rule_result["rule_flags"],
            "ai_score":    ai_result["ai_score"],
            "ai_status":   ai_result["ai_status"],
        },
    }
