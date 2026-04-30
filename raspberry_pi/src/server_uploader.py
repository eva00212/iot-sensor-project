"""
server_uploader.py

Publishes payloads to the oneM2M MQTT broker (mobius.asquare.re.kr:1883).

Reads connection settings from config/site_config.yaml.
Retries on failure up to max_attempts. If all retries fail, the payload
is appended to logs/buffer.jsonl for later retry via flush_buffer().
"""

import json
import logging
import threading
import time
from pathlib import Path

import yaml
from paho.mqtt import publish as mqtt_publish

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "site_config.yaml"

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

_cfg    = _load_config()
_server = _cfg["server"]

HOST         = _server["host"]
PORT         = _server["port"]
KEEPALIVE    = _server.get("keepalive", 60)
CLIENT_ID    = _server.get("client_id", "rpi-uploader")
QOS          = _server.get("qos", 1)
MAX_ATTEMPTS = _server["retry"]["max_attempts"]
RETRY_DELAY  = _server["retry"]["delay"]

# ── Buffer ────────────────────────────────────────────────────────────────────
BUFFER_PATH  = Path(__file__).parent.parent / "logs" / "buffer.jsonl"
_buffer_lock = threading.Lock()


def _write_to_buffer(converted: dict) -> None:
    with _buffer_lock:
        with open(BUFFER_PATH, "a") as f:
            f.write(json.dumps(converted) + "\n")
    logger.warning("Payload buffered to %s", BUFFER_PATH)


def _try_upload(converted: dict) -> bool:
    topic = converted["topic"]
    body  = converted["body"]
    try:
        mqtt_publish.single(
            topic,
            payload=body,
            hostname=HOST,
            port=PORT,
            keepalive=KEEPALIVE,
            client_id=CLIENT_ID,
            qos=QOS,
        )
        logger.info("Published to %s:%d %s", HOST, PORT, topic)
        return True
    except Exception as e:
        logger.warning("Upload failed: %s", e)
        return False


# ── Public API ────────────────────────────────────────────────────────────────
def upload(converted: dict) -> bool:
    """
    Publish to the oneM2M MQTT broker with retries.
    On total failure, appends to buffer.jsonl.

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
        BUFFER_PATH.unlink()

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
