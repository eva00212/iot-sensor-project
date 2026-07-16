"""
simulator.py

Simulates RS485 Modbus sensor readings and feeds them straight into the
processing pipeline (collector.process), without touching real hardware.
There's no local MQTT broker to publish to anymore — the Pi polls the
sensors directly — so this calls the pipeline in-process instead.

Fault injection mirrors modbus_poller.py's real failure behavior: a
simulated fault omits every measurement field (no fabricated 0.0/false
values) and includes device_fault="true" plus error_message/retry_count,
exactly like a real exhausted-retries Modbus failure would.

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
        "wind_direction":  (0.0, 360.0),
        "rainfall":        (0.0, 0.0),      # dry by default; --anomaly occasionally injects rain
        "solar_radiation": (0.0, 800.0),
        "pressure":        (100.0, 102.0),
    },
}

# ── State: previous values for gradual drift ──────────────────────────────────
_prev: dict[str, dict] = {}

def _drift(prev: float, lo: float, hi: float, step: float) -> float:
    """Move value slightly from previous reading, stay within range."""
    delta = random.uniform(-step, step)
    return round(max(lo, min(hi, prev + delta)), 2)

def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def _fault_payload(site_id: str, device_id: str) -> dict:
    """Mirrors a real exhausted-retries Modbus failure: no measurement
    fields at all, just the fault metadata modbus_poller.py would attach."""
    return {
        "site_id":      site_id,
        "device_id":    device_id,
        "timestamp":    _now_iso(),
        "device_fault": "true",
        "error_message": "simulated Modbus failure (--anomaly)",
        "retry_count":  10,
    }

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
        if anomaly_type == "fault":
            return _fault_payload(site_id, device_id)
        if anomaly_type == "temp_high":
            temperature = round(random.uniform(81.0, 90.0), 2)  # past the 80 OUT_OF_RANGE ceiling
        elif anomaly_type == "co2_spike":
            co2 = round(random.uniform(5100.0, 6000.0), 2)

    _prev[device_id] = {"temperature": temperature, "humidity": humidity, "co2": co2}
    return {
        "site_id":      site_id,
        "device_id":    device_id,
        "timestamp":    _now_iso(),
        "temperature":  temperature,
        "humidity":     humidity,
        "co2":          co2,
        "device_fault": "false",
    }

def _generate_outdoor(site_id: str, inject_anomaly: bool) -> dict:
    ranges = NORMAL["outdoor"]
    prev   = _prev.setdefault("device03", {
        "temperature":     random.uniform(*ranges["temperature"]),
        "humidity":        random.uniform(*ranges["humidity"]),
        "wind_speed":      random.uniform(*ranges["wind_speed"]),
        "wind_direction":  random.uniform(*ranges["wind_direction"]),
        "solar_radiation": random.uniform(*ranges["solar_radiation"]),
        "pressure":        random.uniform(*ranges["pressure"]),
    })

    temperature     = _drift(prev["temperature"],     *ranges["temperature"],     step=0.5)
    humidity        = _drift(prev["humidity"],         *ranges["humidity"],        step=1.0)
    wind_speed      = max(0.0, _drift(prev["wind_speed"], *ranges["wind_speed"], step=0.3))
    wind_direction  = round(random.uniform(0.0, 360.0), 1)  # no wraparound-safe drift -- random each cycle
    solar_radiation = max(0.0, _drift(prev["solar_radiation"], *ranges["solar_radiation"], step=30.0))
    pressure        = _drift(prev["pressure"], *ranges["pressure"], step=0.2)
    rainfall        = round(random.uniform(0.5, 3.0), 1) if (inject_anomaly and random.random() < 0.05) else 0.0

    if inject_anomaly and random.random() < 0.1:
        anomaly_type = random.choice(["wind_spike", "solar_over_range", "fault"])
        if anomaly_type == "fault":
            return _fault_payload(site_id, "device03")
        if anomaly_type == "wind_spike":
            wind_speed = round(random.uniform(41.0, 50.0), 2)  # past the 40 OUT_OF_RANGE ceiling
        elif anomaly_type == "solar_over_range":
            solar_radiation = round(random.uniform(1801.0, 2000.0), 2)  # past the 1800 ceiling

    _prev["device03"] = {
        "temperature": temperature, "humidity": humidity, "wind_speed": wind_speed,
        "wind_direction": wind_direction, "solar_radiation": solar_radiation, "pressure": pressure,
    }

    return {
        "site_id":         site_id,
        "device_id":       "device03",
        "timestamp":       _now_iso(),
        "temperature":     temperature,
        "humidity":        humidity,
        "wind_speed":      wind_speed,
        "wind_direction":  wind_direction,
        "rainfall":        rainfall,
        "rain_detected":   "true" if rainfall > 0.0 else "false",
        "solar_radiation": solar_radiation,
        "pressure":        pressure,
        "device_fault":    "false",
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SmartFarm sensor simulator")
    parser.add_argument("--site",     default="testBed01", help="Site ID (default: testBed01)")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between cycles (default: 5)")
    parser.add_argument("--anomaly",  action="store_true", help="Randomly inject anomalies (out-of-range values, rain, and simulated Modbus faults)")
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
