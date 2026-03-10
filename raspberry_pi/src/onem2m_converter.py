"""
onem2m_converter.py

Converts the final payload into oneM2M contentInstance (cin) format
before transmission to the server.

⚠️  The exact structure will be finalized after server-side agreement.
    Adjust RESOURCE_ROOT and the wrapper structure in convert() as needed.

oneM2M resource path:
  /<cse-base>/<ae-name>/<container>/<contentInstance>

Container naming convention:
  {site_id}-{device_id}

Example converted output:
{
  "m2m:cin": {
    "cnf": "application/json",
    "lbl": ["site_01", "indoor_01"],
    "con": "{...}"       ← final payload serialized as JSON string
  }
}
"""

import json
import logging

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
# These will be finalized after server-side agreement.
CSE_BASE  = "id-in"          # CSE base name
AE_NAME   = "smartfarm"      # Application Entity name
CONTENT_FORMAT = "application/json"

# ── Public API ────────────────────────────────────────────────────────────────
def get_container_name(site_id: str, device_id: str) -> str:
    """Returns the oneM2M container name for a given site and device."""
    return f"{site_id}-{device_id}"


def get_resource_path(site_id: str, device_id: str) -> str:
    """Returns the full oneM2M resource path for a contentInstance."""
    container = get_container_name(site_id, device_id)
    return f"/{CSE_BASE}/{AE_NAME}/{container}"


def convert(final_payload: dict) -> dict:
    """
    Wrap the final payload in oneM2M contentInstance format.

    Args:
        final_payload: Output of payload_builder.build()

    Returns a dict with:
      - "resource_path": oneM2M target path for the POST request
      - "body":          oneM2M cin body to send
    """
    site_id   = final_payload["site_id"]
    device_id = final_payload["device_id"]

    resource_path = get_resource_path(site_id, device_id)

    body = {
        "m2m:cin": {
            "cnf": CONTENT_FORMAT,
            "lbl": [site_id, device_id],
            "con": json.dumps(final_payload, ensure_ascii=False),
        }
    }

    logger.debug("[%s / %s] oneM2M path: %s", site_id, device_id, resource_path)

    return {
        "resource_path": resource_path,
        "body":          body,
    }
