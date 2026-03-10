"""
server_uploader.py

Uploads the oneM2M-converted payload to the server via HTTP POST.

Reads connection settings from config/site_config.yaml.
Retries on failure up to max_attempts before dropping the message.
"""

import logging
import time
from pathlib import Path

import requests
import yaml

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "site_config.yaml"

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

_cfg = _load_config()

_server  = _cfg["server"]
BASE_URL = f"{_server['host']}:{_server['port']}"
HEADERS  = _server["headers"]
TIMEOUT  = _server["timeout"]
MAX_ATTEMPTS = _server["retry"]["max_attempts"]
RETRY_DELAY  = _server["retry"]["delay"]

# ── Public API ────────────────────────────────────────────────────────────────
def upload(converted: dict) -> bool:
    """
    POST a oneM2M contentInstance to the server.

    Args:
        converted: Output of onem2m_converter.convert()
                   Must contain 'resource_path' and 'body'.

    Returns True on success, False if all attempts fail.
    """
    resource_path = converted["resource_path"]
    body          = converted["body"]
    url           = f"{BASE_URL}{resource_path}"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                url,
                json=body,
                headers=HEADERS,
                timeout=TIMEOUT,
            )

            if response.status_code in (200, 201):
                logger.info("Uploaded to %s [%d]", url, response.status_code)
                return True

            logger.warning(
                "Upload failed (attempt %d/%d): HTTP %d — %s",
                attempt, MAX_ATTEMPTS, response.status_code, response.text[:200],
            )

        except requests.exceptions.ConnectionError:
            logger.warning("Upload failed (attempt %d/%d): connection error to %s",
                           attempt, MAX_ATTEMPTS, url)
        except requests.exceptions.Timeout:
            logger.warning("Upload failed (attempt %d/%d): timeout after %ds",
                           attempt, MAX_ATTEMPTS, TIMEOUT)
        except requests.exceptions.RequestException as e:
            logger.error("Upload error (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, e)

        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

    logger.error("All %d upload attempts failed for %s. Message dropped.", MAX_ATTEMPTS, url)
    return False
