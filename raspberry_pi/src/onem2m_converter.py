"""
onem2m_converter.py

Maps internal site/device IDs to oneM2M identifiers and builds
the flat JSON payload + MQTT topic for server upload.

MQTT broker : mobius.asquare.re.kr:1883
Topic format: /multisensing/{testBedXX}/{deviceXX}

ID mapping:
  site_01 ~ site_08  →  testBed01 ~ testBed08
  indoor_01           →  device01
  indoor_02           →  device02
  outdoor_01          →  device03
"""

import json
import logging

logger = logging.getLogger(__name__)

SITE_ID_MAP = {f"site_{i:02d}": f"testBed{i:02d}" for i in range(1, 9)}

DEVICE_ID_MAP = {
    "indoor_01":  "device01",
    "indoor_02":  "device02",
    "outdoor_01": "device03",
}

TOPIC_BASE = "/multisensing"


def convert(final_payload: dict) -> dict:
    """
    Convert the final internal payload to oneM2M MQTT format.

    Args:
        final_payload: Output of payload_builder.build()

    Returns a dict with:
      - "topic": MQTT topic to publish to
      - "body":  JSON string payload for the server
    """
    internal_site   = final_payload["site_id"]
    internal_device = final_payload["device_id"]

    site_id   = SITE_ID_MAP.get(internal_site)
    device_id = DEVICE_ID_MAP.get(internal_device)

    if site_id is None:
        raise ValueError(f"Unknown site_id: '{internal_site}'")
    if device_id is None:
        raise ValueError(f"Unknown device_id: '{internal_device}'")

    topic = f"{TOPIC_BASE}/{site_id}/{device_id}"

    body = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": final_payload["timestamp"],
        **final_payload["data"],
    }

    logger.debug("[%s / %s] → topic: %s", internal_site, internal_device, topic)

    return {
        "topic": topic,
        "body":  json.dumps(body, ensure_ascii=False),
    }
