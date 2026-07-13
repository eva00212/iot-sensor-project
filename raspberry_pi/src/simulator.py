"""
simulator.py

Simulates RS485 Modbus sensor readings and feeds them straight into the
processing pipeline (collector.process), without touching real hardware.
There's no local MQTT broker to publish to anymore — the Pi polls the
sensors directly — so this calls the pipeline in-process instead.

Usage:
  python src/simulator.py                        # default: testBed01, all devices
  python src/simulator.py --site testBed02        # specific site
  python src/simulator.py --interval 3            # run a cycle every 3 seconds
  python src/simulator.py --anomaly               # inject anomalies randomly
"""

import argparse
import random
import time
from datetime import datetime

import collector

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
        "device_fault": "true" if device_fault else "false",
    }

def _generate_outdoor(site_id: str, inject_anomaly: bool) -> dict:
    ranges = NORMAL["outdoor"]
    prev   = _prev.setdefault("device03", {
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

    _prev["device03"] = {
        "temperature": temperature, "humidity": humidity,
        "wind_speed": wind_speed,   "solar_radiation": solar_radiation,
    }

    return {
        "site_id":         site_id,
        "device_id":       "device03",
        "timestamp":       datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "temperature":     temperature,
        "humidity":        humidity,
        "wind_speed":      wind_speed,
        "solar_radiation": solar_radiation,
        "rain_detected":   "true" if random.random() < 0.05 else "false",
        "device_fault":    "true" if device_fault else "false",
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SmartFarm sensor simulator")
    parser.add_argument("--site",     default="testBed01", help="Site ID (default: testBed01)")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between cycles (default: 5)")
    parser.add_argument("--anomaly",  action="store_true", help="Randomly inject anomalies")
    args = parser.parse_args()

    print(f"Simulator started — site: {args.site}, interval: {args.interval}s, anomalies: {args.anomaly}")
    print("Feeding synthetic readings straight into the pipeline (no MQTT). Press Ctrl+C to stop.\n")

    try:
        while True:
            payloads = [
                _generate_indoor("device01", args.site, args.anomaly),
                _generate_indoor("device02", args.site, args.anomaly),
                _generate_outdoor(args.site, args.anomaly),
            ]

            for payload in payloads:
                print(f"[{payload['timestamp']}] {payload['site_id']} / {payload['device_id']}")
                print(f"  {payload}\n")
                collector.process(payload)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("Simulator stopped.")

if __name__ == "__main__":
    main()
