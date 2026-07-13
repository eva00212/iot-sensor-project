"""
onem2m_converter.py

Builds the flat JSON payload + MQTT topic for server upload.

MQTT broker : mobius.asquare.re.kr:1883
Topic format: /multisensing/{site_id}/{device_id}
              (site_id values use the testBed01..testBed08 format)
"""

import json
import logging

logger = logging.getLogger(__name__)

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
    site_id   = final_payload["site_id"]
    device_id = final_payload["device_id"]

    topic = f"{TOPIC_BASE}/{site_id}/{device_id}"

    body = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": final_payload["timestamp"],
        **final_payload["data"],
    }

    logger.debug("[%s / %s] -> topic: %s", site_id, device_id, topic)

    return {
        "topic": topic,
        "body":  json.dumps(body, ensure_ascii=False),
    }
