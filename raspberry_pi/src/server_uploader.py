"""
server_uploader.py

Uploads the oneM2M-converted payload to the server via HTTP POST.

Reads connection settings from config/site_config.yaml.
Retries on failure up to max_attempts. If all retries fail, the payload
is appended to logs/buffer.jsonl for later retry via flush_buffer().
"""

import json
import logging
import threading
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

# ── Buffer ────────────────────────────────────────────────────────────────────
BUFFER_PATH = Path(__file__).parent.parent / "logs" / "buffer.jsonl"
_buffer_lock = threading.Lock()

def _write_to_buffer(converted: dict) -> None:
    with _buffer_lock:
        with open(BUFFER_PATH, "a") as f:
            f.write(json.dumps(converted) + "\n")
    logger.warning("Payload buffered to %s", BUFFER_PATH)

# ── Internal upload (single attempt) ─────────────────────────────────────────
def _try_upload(converted: dict) -> bool:
    resource_path = converted["resource_path"]
    body          = converted["body"]
    url           = f"{BASE_URL}{resource_path}"
    try:
        response = requests.post(url, json=body, headers=HEADERS, timeout=TIMEOUT)
        if response.status_code in (200, 201):
            logger.info("Uploaded to %s [%d]", url, response.status_code)
            return True
        logger.warning("Upload failed: HTTP %d — %s", response.status_code, response.text[:200])
    except requests.exceptions.ConnectionError:
        logger.warning("Upload failed: connection error to %s", url)
    except requests.exceptions.Timeout:
        logger.warning("Upload failed: timeout after %ds", TIMEOUT)
    except requests.exceptions.RequestException as e:
        logger.error("Upload error: %s", e)
    return False

# ── Public API ────────────────────────────────────────────────────────────────
def upload(converted: dict) -> bool:
    """
    POST a oneM2M contentInstance to the server with retries.
    On total failure, appends to buffer.jsonl instead of dropping.

    Returns True on success, False if buffered.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if _try_upload(converted):
            return True
        logger.warning("Upload attempt %d/%d failed", attempt, MAX_ATTEMPTS)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

    logger.error("All %d upload attempts failed. Buffering payload.", MAX_ATTEMPTS)
    _write_to_buffer(converted)
    return False


def flush_buffer() -> None:
    """
    Retry all payloads stored in buffer.jsonl.
    Successfully uploaded records are removed; failed ones are kept.
    Called periodically by a background scheduler in mqtt_collector.py.
    """
    if not BUFFER_PATH.exists():
        return

    with _buffer_lock:
        lines = BUFFER_PATH.read_text().splitlines()
        BUFFER_PATH.unlink()  # clear atomically before retrying

    if not lines:
        return

    logger.info("Flushing %d buffered payload(s)...", len(lines))
    failed = []
    for line in lines:
        try:
            if not _try_upload(json.loads(line)):
                failed.append(line)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Discarding malformed buffer entry: %s", e)

    if failed:
        with _buffer_lock:
            with open(BUFFER_PATH, "a") as f:
                f.write("\n".join(failed) + "\n")
        logger.warning("%d payload(s) re-buffered after flush.", len(failed))
    else:
        logger.info("Buffer flush complete. All payloads uploaded.")
