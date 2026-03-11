"""
mqtt_collector.py

Subscribes to smartfarm/+/+/raw and receives raw sensor payloads
from all indoor/outdoor nodes.

Received messages are parsed and passed to the processing pipeline:
  data_validator → anomaly_rules → anomaly_ai → payload_builder → server_uploader
"""

import json
import logging
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

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
CONFIG_PATH = Path(__file__).parent.parent / "config" / "mqtt_config.yaml"

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ── Pipeline placeholder (replace with real imports as modules are built) ─────
import data_validator
import anomaly_rules
import anomaly_ai
import payload_builder
import onem2m_converter
import server_uploader

def process(payload: dict) -> None:
    """Entry point for the full processing pipeline."""
    site_id   = payload.get("site_id")
    device_id = payload.get("device_id")
    logger.info("[%s / %s] received: %s", site_id, device_id, payload)

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

# ── MQTT Callbacks ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        topic = userdata["topic"]
        qos   = userdata["qos"]
        client.subscribe(topic, qos)
        logger.info("Connected to broker. Subscribed to '%s' (QoS %d)", topic, qos)
    else:
        logger.error("Connection failed with code %s", reason_code)

def on_disconnect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        logger.warning("Unexpected disconnect (rc=%s). Will reconnect...", reason_code)

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("Failed to parse message from '%s': %s", msg.topic, e)
        return

    process(payload)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    cfg    = load_config()
    broker = cfg["broker"]
    sub    = cfg["subscribe"]
    client_cfg = cfg["client"]

    userdata = {"topic": sub["topic"], "qos": sub["qos"]}

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_cfg["id"], userdata=userdata)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    reconnect_delay = client_cfg["reconnect_delay"]

    while True:
        try:
            client.connect(broker["host"], broker["port"], broker["keepalive"])
            client.loop_forever()
        except ConnectionRefusedError:
            logger.error("Broker not reachable. Retrying in %ds...", reconnect_delay)
            time.sleep(reconnect_delay)
        except KeyboardInterrupt:
            logger.info("Shutting down collector.")
            client.disconnect()
            break

if __name__ == "__main__":
    main()
