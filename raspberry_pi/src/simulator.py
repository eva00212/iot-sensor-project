"""
simulator.py

Simulates Arduino sensor nodes publishing MQTT messages.
Use this to test the full pipeline without physical hardware.

Publishes to:
  smartfarm/{site_id}/{device_id}/raw

Usage:
  python src/simulator.py                        # default: site_01, all devices
  python src/simulator.py --site site_02         # specific site
  python src/simulator.py --interval 3           # publish every 3 seconds
  python src/simulator.py --anomaly              # inject anomalies randomly
"""

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "mqtt_config.yaml"

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ── Baseline sensor ranges (normal operating values) ─────────────────────────
NORMAL = {
    "indoor": {
        "temperature": (20.0, 28.0),
        "humidity":    (40.0, 70.0),
        "co2":         (400.0, 1000.0),
    },
    "outdoor": {
        "temperature":     (10.0, 35.0),
        "humidity":        (30.0, 80.0),
        "wind_speed":      (0.0, 5.0),
        "solar_radiation": (0.0, 800.0),
    },
}

# ── State: previous values for gradual drift ──────────────────────────────────
_prev: dict[str, dict] = {}

def _drift(prev: float, lo: float, hi: float, step: float) -> float:
    """Move value slightly from previous reading, stay within range."""
    delta = random.uniform(-step, step)
    return round(max(lo, min(hi, prev + delta)), 2)

def _generate_indoor(device_id: str, site_id: str, inject_anomaly: bool) -> dict:
    ranges = NORMAL["indoor"]
    prev   = _prev.setdefault(device_id, {
        "temperature": random.uniform(*ranges["temperature"]),
        "humidity":    random.uniform(*ranges["humidity"]),
        "co2":         random.uniform(*ranges["co2"]),
    })

    temperature = _drift(prev["temperature"], *ranges["temperature"], step=0.5)
    humidity    = _drift(prev["humidity"],    *ranges["humidity"],    step=1.0)
    co2         = _drift(prev["co2"],         *ranges["co2"],         step=20.0)

    if inject_anomaly and random.random() < 0.1:
        anomaly_type = random.choice(["temp_high", "co2_spike", "fault"])
        if anomaly_type == "temp_high":
            temperature = round(random.uniform(61.0, 70.0), 2)
        elif anomaly_type == "co2_spike":
            co2 = round(random.uniform(5100.0, 6000.0), 2)
        elif anomaly_type == "fault":
            return _make_payload(site_id, device_id,
                                 temperature, humidity, co2, device_fault=True)

    _prev[device_id] = {"temperature": temperature, "humidity": humidity, "co2": co2}
    return _make_payload(site_id, device_id, temperature, humidity, co2)

def _make_payload(site_id, device_id, temperature, humidity, co2, device_fault=False):
    return {
        "site_id":      site_id,
        "device_id":    device_id,
        "timestamp":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "temperature":  temperature,
        "humidity":     humidity,
        "co2":          co2,
        "device_fault": device_fault,
    }

def _generate_outdoor(site_id: str, inject_anomaly: bool) -> dict:
    ranges = NORMAL["outdoor"]
    prev   = _prev.setdefault("outdoor_01", {
        "temperature":     random.uniform(*ranges["temperature"]),
        "humidity":        random.uniform(*ranges["humidity"]),
        "wind_speed":      random.uniform(*ranges["wind_speed"]),
        "solar_radiation": random.uniform(*ranges["solar_radiation"]),
    })

    temperature     = _drift(prev["temperature"],     *ranges["temperature"],     step=0.5)
    humidity        = _drift(prev["humidity"],         *ranges["humidity"],        step=1.0)
    wind_speed      = _drift(prev["wind_speed"],       *ranges["wind_speed"],      step=0.3)
    solar_radiation = _drift(prev["solar_radiation"],  *ranges["solar_radiation"], step=30.0)
    wind_speed      = max(0.0, wind_speed)
    solar_radiation = max(0.0, solar_radiation)

    device_fault = False
    if inject_anomaly and random.random() < 0.1:
        anomaly_type = random.choice(["wind_spike", "solar_negative", "fault"])
        if anomaly_type == "wind_spike":
            wind_speed = round(random.uniform(20.0, 30.0), 2)
        elif anomaly_type == "solar_negative":
            solar_radiation = round(random.uniform(-50.0, -1.0), 2)
        elif anomaly_type == "fault":
            device_fault = True

    _prev["outdoor_01"] = {
        "temperature": temperature, "humidity": humidity,
        "wind_speed": wind_speed,   "solar_radiation": solar_radiation,
    }

    return {
        "site_id":         site_id,
        "device_id":       "outdoor_01",
        "timestamp":       datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "temperature":     temperature,
        "humidity":        humidity,
        "wind_speed":      wind_speed,
        "solar_radiation": solar_radiation,
        "rain_detected":   random.random() < 0.05,
        "device_fault":    device_fault,
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SmartFarm sensor simulator")
    parser.add_argument("--site",     default="site_01", help="Site ID (default: site_01)")
    parser.add_argument("--interval", type=float, default=5.0, help="Publish interval in seconds (default: 5)")
    parser.add_argument("--anomaly",  action="store_true", help="Randomly inject anomalies")
    args = parser.parse_args()

    cfg    = load_config()
    broker = cfg["broker"]

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="simulator")
    client.connect(broker["host"], broker["port"], broker["keepalive"])
    client.loop_start()

    print(f"Simulator started — site: {args.site}, interval: {args.interval}s, anomalies: {args.anomaly}")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            payloads = [
                _generate_indoor("indoor_01", args.site, args.anomaly),
                _generate_indoor("indoor_02", args.site, args.anomaly),
                _generate_outdoor(args.site, args.anomaly),
            ]

            for payload in payloads:
                device_id = payload["device_id"]
                topic     = f"smartfarm/{args.site}/{device_id}/raw"
                client.publish(topic, json.dumps(payload), qos=1)
                print(f"[{payload['timestamp']}] {topic}")
                print(f"  {json.dumps(payload, ensure_ascii=False)}\n")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("Simulator stopped.")
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
