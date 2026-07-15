"""
collector.py

Polls the 3 RS485 Modbus RTU sensors wired directly to this Raspberry Pi
(via modbus_poller) and runs each reading through the processing pipeline:
  data_validator → anomaly_rules → anomaly_ai → payload_builder →
  onem2m_converter → server_uploader

server_uploader publishes the processed result to the oneM2M MQTT broker —
that outbound upload is the only MQTT hop in this system.
"""

import logging
import threading
import time
from pathlib import Path

import yaml

import modbus_poller

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "collector.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MODBUS_CONFIG_PATH = Path(__file__).parent.parent / "config" / "modbus_config.yaml"
SITE_CONFIG_PATH   = Path(__file__).parent.parent / "config" / "site_config.yaml"

def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

_modbus_cfg = load_config(MODBUS_CONFIG_PATH)
POLL_INTERVAL_SEC    = _modbus_cfg["poll_interval_ms"] / 1000
INTER_POLL_DELAY_SEC = _modbus_cfg["inter_poll_delay_ms"] / 1000

SITE_ID = load_config(SITE_CONFIG_PATH)["site_id"]

# ── Pipeline ─────────────────────────────────────────────────────────────────
import data_validator
import anomaly_rules
import anomaly_ai
import payload_builder
import onem2m_converter
import server_uploader

def process(payload: dict) -> None:
    """Entry point for the full processing pipeline.

    Wrapped in a broad try/except: an unexpected exception in any pipeline
    stage (validation passed a shape a later stage didn't expect, a
    transient anomaly-model error, etc.) must only drop this one reading,
    never crash the whole collector -- the other two sensors on the bus
    still need to keep being polled and uploaded.
    """
    site_id   = payload.get("site_id")
    device_id = payload.get("device_id")
    logger.info("[%s / %s] polled: %s", site_id, device_id, payload)

    try:
        validated = data_validator.safe_validate(payload)
        if validated is None:
            return

        rule_result = anomaly_rules.check(validated)
        if rule_result["rule_flags"]:
            logger.warning("[%s / %s] Rule flags: %s", site_id, device_id, rule_result["rule_flags"])

        ai_result = anomaly_ai.score(validated)
        if ai_result["ai_status"] == "anomaly":
            logger.warning("[%s / %s] AI anomaly score: %s", site_id, device_id, ai_result["ai_score"])

        final = payload_builder.build(validated, rule_result, ai_result)
        converted = onem2m_converter.convert(final)
        server_uploader.upload(converted)
    except Exception:
        logger.exception("[%s / %s] Unexpected error in processing pipeline; dropping this reading.", site_id, device_id)

# ── Missing Data Scheduler ────────────────────────────────────────────────────
def _missing_data_loop():
    """Background thread: checks for silent devices every 30 seconds."""
    while True:
        time.sleep(30)
        missing = anomaly_rules.check_missing_data()
        for (site_id, device_id), flags in missing.items():
            logger.warning("[%s / %s] %s", site_id, device_id, flags)

# ── Buffer Flush Scheduler ────────────────────────────────────────────────────
def _buffer_flush_loop():
    """Background thread: retries buffered payloads every 5 minutes."""
    while True:
        time.sleep(300)
        server_uploader.flush_buffer()

# ── Polling Loop ───────────────────────────────────────────────────────────────
def _poll_cycle(site_id: str) -> None:
    """Sequentially polls device01, device02, device03 with a minimum gap
    between each, running every reading through the processing pipeline."""
    process(modbus_poller.poll_device01(site_id))
    time.sleep(INTER_POLL_DELAY_SEC)

    process(modbus_poller.poll_device02(site_id))
    time.sleep(INTER_POLL_DELAY_SEC)

    process(modbus_poller.poll_device03(site_id))

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    server_uploader.start()

    threading.Thread(target=_missing_data_loop, daemon=True).start()
    logger.info("Missing data scheduler started (interval: 30s)")

    threading.Thread(target=_buffer_flush_loop, daemon=True).start()
    logger.info("Buffer flush scheduler started (interval: 300s)")

    logger.info(
        "Starting poll loop for site '%s' (interval: %.0fs, inter-device gap: %.0fms)",
        SITE_ID, POLL_INTERVAL_SEC, INTER_POLL_DELAY_SEC * 1000,
    )

    try:
        while True:
            cycle_start = time.monotonic()
            _poll_cycle(SITE_ID)
            elapsed = time.monotonic() - cycle_start
            time.sleep(max(0.0, POLL_INTERVAL_SEC - elapsed))
    except KeyboardInterrupt:
        logger.info("Shutting down collector.")
        server_uploader.stop()

if __name__ == "__main__":
    main()
