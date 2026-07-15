# Project: SmartFarm Multi-Site Environmental Monitoring System

## Overview
A test-bed-based environmental monitoring system. Two indoor sensors and one outdoor sensor are wired directly to a Raspberry Pi 5's RS485 interface over a shared RS485 bus (Modbus RTU, unique slave address per sensor) — there is no microcontroller in between. The Raspberry Pi itself polls all three sensors, then performs data validation and anomaly detection locally before sending the processed data to the server over MQTT (oneM2M). The Pi reaches the internet over an LTE modem/router, not wired Ethernet or WiFi — the uplink can be intermittent, so uploads are built to tolerate outages rather than assume a stable link.

## Tech Stack
- Language: Python
- Framework: paho-mqtt, minimalmodbus
- Database: TBD (server side)
- Edge Device: Raspberry Pi 5 (with an attached RS485 interface and an LTE modem/router for uplink)
- Communication: RS485 Modbus RTU (sensors ↔ Pi), MQTT/oneM2M over LTE (Pi ↔ server)
- AI: optional anomaly scoring model

## Project Structure
```text
iot-sensor-project/
├── raspberry_pi/
│   ├── src/
│   │   ├── collector.py          # polls sensors, drives the processing pipeline
│   │   ├── modbus_poller.py      # RS485 Modbus RTU engine (reads device01/02/03)
│   │   ├── data_validator.py     # data validation
│   │   ├── anomaly_rules.py      # rule-based anomaly detection
│   │   ├── anomaly_ai.py         # AI anomaly scoring
│   │   ├── payload_builder.py    # build payload for server
│   │   ├── onem2m_converter.py   # oneM2M conversion
│   │   ├── server_uploader.py    # upload to server (oneM2M MQTT)
│   │   └── simulator.py          # feeds synthetic readings into the pipeline, no hardware needed
│   │
│   ├── config/
│   │   ├── site_config.yaml           # this Pi's site_id + server upload settings (gitignored, real secrets)
│   │   ├── site_config.example.yaml   # safe template — copy to site_config.yaml and edit
│   │   ├── modbus_config.yaml         # RS485 serial port + polling settings
│   │   └── rule_config.yaml           # anomaly rule configuration
│   │
│   ├── tests/                    # unit tests (modbus_poller parsing/CRC logic)
│   ├── logs/                     # runtime logs
│   └── service/                  # systemd service files
│
└── docs/                         # system documentation
```

## Code Style Rules
- Each deployment test bed must use a unique `site_id` (format: `testBedXX`)
- Each device must use a unique `device_id`
- MQTT topics must include `site_id + device_id`
- JSON payload fields must use `snake_case`
- Every payload must include `site_id`, `device_id`, and `timestamp`
- Sensor read failures must be handled with exceptions
- Modbus polling must handle CRC validation, timeouts, and retries
- The server uploader must hold one persistent MQTT connection with automatic reconnect + backoff, never a blocking retry loop in the poll path
- Unsent payloads must be queued to disk during an outage and retransmitted without duplication once the link returns
- Data validation and anomaly detection must run on the Raspberry Pi
- Sensors expose raw values via Modbus registers only — no on-sensor processing
- The server must always be addressed by hostname, never a hardcoded IP or network interface — LTE connections are NAT'd and the Pi's own address can change
- oneM2M formatting will be finalized after server-side agreement

## Commands
- `raspberry_pi/install.sh` - full unattended deploy on a fresh Raspberry Pi OS install: system packages, venv, Python deps, UART enablement (reboots and resumes itself if needed), systemd service registration, and `site_config.yaml` scaffolding. The only manual step after this is editing `config/site_config.yaml`.
- `pip install -r raspberry_pi/requirements.txt` - install Raspberry Pi dependencies (already done by install.sh; useful standalone for local/dev work)
- `python src/collector.py` - run the sensor collector (polls RS485, runs the pipeline)
- `python src/simulator.py` - exercise the pipeline with synthetic readings, no hardware needed
- `systemctl start sensor-collector` - start service
- `systemctl enable sensor-collector` - enable service at boot

## Important Notes
- Each test bed consists of 3 RS485 Modbus RTU sensors (device01, device02, device03) wired directly to 1 Raspberry Pi 5's RS485 interface — no microcontroller in between
- The Raspberry Pi itself polls each sensor over RS485 (unique Modbus slave address per sensor); MQTT is only used for the outbound upload to the oneM2M server
- The same system structure is deployed to multiple test beds
- All test bed data is transmitted to a single central server
- Data conflicts are prevented using `site_id + device_id`
- Anomaly detection is performed locally on the Raspberry Pi
- AI models are used only as supplementary analysis
- oneM2M conversion is applied before server transmission
- `device_fault` reflects a failed Modbus poll (timeout, CRC error, or exhausted retries) for that sensor, not a voltage reading
- Illumination is not used because there is no dedicated illumination sensor
- Solar radiation is used as the outdoor light-related measurement field
- Sensor polling (RS485) and the LTE uplink are independent failure domains: an LTE outage never blocks or slows sensor polling — readings queue to disk and drain once the link returns

## System Architecture
```text
[ device01 @ 0x01 ] \
[ device02 @ 0x02 ]  --- shared RS485 Modbus RTU bus ---> [ Raspberry Pi 5 ]
[ device03 @ 0x03 ] /                                      |
                                                            | LTE
                                                            v
                                                   [ MQTT / oneM2M server ]

Raspberry Pi
   │
   ├── Modbus RTU Polling (modbus_poller.py)
   ├── Data Validation
   ├── Rule-based Anomaly Detection
   ├── AI-based Anomaly Score (optional)
   ├── Final Payload Build
   ├── oneM2M Conversion
   └── Server Upload (persistent MQTT connection, reconnect + backoff, disk-buffered on outage)
```

## Deployment Notes (LTE Uplink)
- The Pi's internet connection is an LTE modem/router. Treat it as intermittent
  by default — the service must start and keep running even if LTE isn't up
  yet at boot, and must recover on its own once it is.
- `server_uploader.py` holds one persistent MQTT connection and uses paho's
  built-in `reconnect_delay_set()` for exponential-backoff reconnect — there is
  no hand-rolled reconnect loop, and no blocking retry-with-sleep in the poll
  path. `sensor-collector.service` uses `Wants=network-online.target` (soft) +
  `Restart=always`, so a missing/late LTE link never prevents the service from
  starting or staying up.
- Readings that can't be confirmed delivered (disconnected, or unconfirmed
  within `publish_timeout_seconds`) are queued to `logs/buffer.jsonl` and
  retransmitted once the link returns — both immediately on reconnect and on a
  periodic schedule. Buffered messages are removed one at a time, immediately
  after each confirmed send, to avoid duplicate retransmission if the process
  restarts mid-flush.
- The server is always addressed by hostname (`site_config.yaml`'s
  `server.host`), never a hardcoded IP or network interface — LTE links are
  typically NAT'd and the Pi's own address can change between sessions.
- Each deployed Pi's MQTT `client_id` is auto-suffixed with its `site_id` so
  multiple test-bed Pis sharing the same `site_config.example.yaml` template
  never collide on the broker (only one connection per client_id is allowed).

## Sensor Fields

### Common Sensors (Indoor / Outdoor)
- temperature
- humidity

### Indoor Sensors
- co2

### Outdoor Sensors
- wind_speed
- rain_detected
- solar_radiation

### Device Status
- device_fault

## Node Configuration

### device01 (indoor)
- temperature
- humidity
- co2
- device_fault

### device02 (indoor)
- temperature
- humidity
- co2
- device_fault

### device03 (outdoor)
- temperature
- humidity
- wind_speed
- rain_detected
- solar_radiation
- device_fault

## Modbus Register Map
All three sensors are wired directly to the Raspberry Pi's RS485 interface
over one shared bus (4800 baud, 8N1), each with its own Modbus slave
address. `modbus_poller.py` polls every sensor with Modbus function code
`0x03` (Read Holding Registers); CRC validation and RTU framing are handled
by the `minimalmodbus` library, with application-level retry/timeout
handling on top.

| Slave addr | Device     | Registers read |
|------------|------------|-----------------|
| `0x01`     | `device01` | 504–507 |
| `0x02`     | `device02` | 504–507 |
| `0x03`     | `device03` | 500–515 |

| Register | Field | Scaling | Notes |
|----------|-------|---------|-------|
| 500 | wind_speed | raw × 0.1 m/s | device03 only |
| 504 | humidity | raw × 0.1 %RH | |
| 505 | temperature | raw × 0.1 °C | signed 16-bit (two's complement) |
| 507 | co2 | raw integer, ppm | device01/device02 only (CO2 variant) |
| 513 | rainfall amount | raw × 0.1 mm | internal only — not sent in the payload. Used to derive `rain_detected = rainfall > 0` |
| 515 | solar_radiation | raw value, W/m² | device03 only |

`device01`/`device02` and `device03` are the same sensor family sharing one
register table — each variant only populates the registers for the sensors
it has installed, so the board block-reads the full register span for its
device type and only extracts the fields relevant to it.

## MQTT Topic Structure
```text
/multisensing/{site_id}/{device_id}
```
(`site_id` values use the `testBed01`..`testBed08` format)

### Example
```text
/multisensing/testBed01/device01
/multisensing/testBed01/device02
/multisensing/testBed01/device03
```

## Example Raw Payload (Indoor Node)
```json
{
  "site_id": "testBed01",
  "device_id": "device01",
  "timestamp": "2026-03-10T12:10:21",
  "temperature": 24.6,
  "humidity": 63.2,
  "co2": 512,
  "device_fault": "false"
}
```

## Example Raw Payload (Outdoor Node)
```json
{
  "site_id": "testBed01",
  "device_id": "device03",
  "timestamp": "2026-03-10T12:10:21",
  "temperature": 23.8,
  "humidity": 61.4,
  "wind_speed": 1.4,
  "rain_detected": "false",
  "solar_radiation": 520.0,
  "device_fault": "false"
}
```

## Data Processing Order

### Step 1. Sensor Polling
The Raspberry Pi sequentially polls device01, device02, and device03 over
the shared RS485 bus using Modbus RTU (function code `0x03`), with CRC
validation, timeouts, and retries. A failed poll produces a payload with
`device_fault: "true"`.

### Step 2. Data Validation
- Validate required fields
- Check data types
- Verify timestamp
- Filter malformed or corrupted data

### Step 3. Rule-based Anomaly Detection
- Missing data detection
- Range validation
- Sudden change detection
- device01 vs device02 comparison
- device_fault status check

### Step 4. AI-based Anomaly Analysis (Optional)
Unsupervised anomaly scoring model (e.g., Isolation Forest) calculates anomaly scores.

### Step 5. Final Payload Build
Combine:
- raw sensor data
- rule-based anomaly results
- AI anomaly score
- metadata (`site_id`, `device_id`, `timestamp`)

### Step 6. oneM2M Conversion
The final payload is wrapped into oneM2M format before transmission.

### Step 7. Server Upload
The converted payload is transmitted to the server over MQTT.

## Anomaly Detection Strategy

### 1st Stage: Rule-based Detection

#### Missing Data
- sensor data missing for a predefined period
- repeated identical timestamp

#### Out-of-Range Detection

Example baseline:

temperature  
-20 ~ 60

humidity  
0 ~ 100

co2  
0 ~ 5000

wind_speed  
>= 0

solar_radiation  
>= 0

#### Sudden Change Detection
Detect rapid changes in:
- temperature
- humidity
- co2
- wind_speed
- solar_radiation

#### Cross-check Between Indoor Nodes
Compare device01 and device02:
- temperature difference
- humidity difference
- co2 difference

#### Device Status
- `device_fault == true` (set when a Modbus poll fails after all retries — timeout or CRC error)

### 2nd Stage: AI-based Detection
AI-based anomaly score using models such as Isolation Forest.

Used for:
- long-term drift detection
- abnormal pattern detection
- rule-based decision support

## Example Final Payload Before oneM2M Conversion
```json
{
  "site_id": "testBed01",
  "device_id": "device01",
  "timestamp": "2026-03-10T12:10:21",
  "data": {
    "temperature": 24.6,
    "humidity": 63.2,
    "co2": 512,
    "device_fault": "false"
  },
  "anomaly": {
    "rule_status": "normal",
    "rule_flags": [],
    "ai_score": 0.12,
    "ai_status": "normal"
  }
}
```

## Test Bed ID Rules (`site_id` values)
```text
testBed01
testBed02
testBed03
testBed04
testBed05
testBed06
testBed07
testBed08
```

## Device ID Rules
```text
device01   (indoor)
device02   (indoor)
device03   (outdoor)
```

### Final Identification Rule
```text
site_id + device_id
```

### Example
```text
testBed01 / device01
testBed01 / device02
testBed01 / device03

testBed02 / device01
testBed02 / device02
testBed02 / device03
```

## TODO
- [x] implement Raspberry Pi Modbus RTU collector
- [x] implement data validation module
- [x] define rule-based anomaly detection thresholds
- [x] implement AI anomaly scoring module
- [x] finalize oneM2M payload format
- [x] implement server upload interface
- [x] register systemd service