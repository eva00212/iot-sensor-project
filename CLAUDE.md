# Project: SmartFarm Multi-Site Environmental Monitoring System

## Overview
A site-based environmental monitoring system consisting of two indoor sensor nodes and one outdoor sensor node. Sensor data is transmitted via WiFi using MQTT to a Raspberry Pi collector, where data validation and anomaly detection are performed before sending the processed data to the server.

## Tech Stack
- Language: Arduino C++, Python
- Framework: paho-mqtt
- Database: TBD (server side)
- Edge Device: Raspberry Pi
- Communication: MQTT
- AI: optional anomaly scoring model

## Project Structure
```text
iot-sensor-project/
├── arduino/
│   ├── indoor_node/              # indoor sensor node firmware
│   └── outdoor_node/             # outdoor sensor node firmware
│
├── raspberry_pi/
│   ├── src/
│   │   ├── mqtt_collector.py     # MQTT subscriber
│   │   ├── data_validator.py     # data validation
│   │   ├── anomaly_rules.py      # rule-based anomaly detection
│   │   ├── anomaly_ai.py         # AI anomaly scoring
│   │   ├── payload_builder.py    # build payload for server
│   │   ├── onem2m_converter.py   # oneM2M conversion
│   │   └── server_uploader.py    # upload to server
│   │
│   ├── config/
│   │   ├── site_config.yaml      # site configuration
│   │   ├── mqtt_config.yaml      # MQTT configuration
│   │   └── rule_config.yaml      # anomaly rule configuration
│   │
│   ├── logs/                     # runtime logs
│   └── service/                  # systemd service files
│
└── docs/                         # system documentation
```

## Code Style Rules
- Each deployment site must use a unique `site_id`
- Each device must use a unique `device_id`
- MQTT topics must include `site_id + device_id`
- JSON payload fields must use `snake_case`
- Every payload must include `site_id`, `device_id`, and `timestamp`
- Sensor read failures must be handled with exceptions
- MQTT reconnect logic must be implemented
- Data validation and anomaly detection must run on the Raspberry Pi
- Sensor nodes must only transmit raw sensor data
- oneM2M formatting will be finalized after server-side agreement

## Commands
- `arduino ide` - build and upload sensor firmware
- `pip install paho-mqtt scikit-learn` - install Raspberry Pi dependencies
- `python src/mqtt_collector.py` - run data collector
- `systemctl start sensor-collector` - start service
- `systemctl enable sensor-collector` - enable service at boot

## Important Notes
- Each site consists of 3 sensor nodes and 1 Raspberry Pi
- The same system structure is deployed to multiple sites
- All site data is transmitted to a single central server
- Data conflicts are prevented using `site_id + device_id`
- Anomaly detection is performed locally on the Raspberry Pi
- AI models are used only as supplementary analysis
- oneM2M conversion is applied before server transmission
- Voltage is used only for internal anomaly detection logic and is not included in the server payload
- Illumination is not used because there is no dedicated illumination sensor
- Solar radiation is used as the outdoor light-related measurement field

## System Architecture
```text
[ indoor_01 ] \
[ indoor_02 ]  ---> MQTT ---> [ Raspberry Pi ]
[ outdoor_01 ] /

Raspberry Pi
   │
   ├── Data Validation
   ├── Rule-based Anomaly Detection
   ├── AI-based Anomaly Score (optional)
   ├── Final Payload Build
   ├── oneM2M Conversion
   └── Server Upload
```

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

### indoor_01
- temperature
- humidity
- co2
- device_fault

### indoor_02
- temperature
- humidity
- co2
- device_fault

### outdoor_01
- temperature
- humidity
- wind_speed
- rain_detected
- solar_radiation
- device_fault

## MQTT Topic Structure
```text
smartfarm/{site_id}/{device_id}/raw
```

### Example
```text
smartfarm/site_01/indoor_01/raw
smartfarm/site_01/indoor_02/raw
smartfarm/site_01/outdoor_01/raw
```

## Example Raw Payload (Indoor Node)
```json
{
  "site_id": "site_01",
  "device_id": "indoor_01",
  "timestamp": "2026-03-10T12:10:21",
  "temperature": 24.6,
  "humidity": 63.2,
  "co2": 512,
  "device_fault": false
}
```

## Example Raw Payload (Outdoor Node)
```json
{
  "site_id": "site_01",
  "device_id": "outdoor_01",
  "timestamp": "2026-03-10T12:10:21",
  "temperature": 23.8,
  "humidity": 61.4,
  "wind_speed": 1.4,
  "rain_detected": false,
  "solar_radiation": 520.0,
  "device_fault": false
}
```

## Data Processing Order

### Step 1. Sensor Data Publish
Sensor nodes measure environmental data and publish raw JSON messages to MQTT.

### Step 2. MQTT Receive on Raspberry Pi
The Raspberry Pi subscribes to MQTT topics and receives data from all sensor nodes.

### Step 3. Data Validation
- Validate required fields
- Check data types
- Verify timestamp
- Filter malformed or corrupted data

### Step 4. Rule-based Anomaly Detection
- Missing data detection
- Range validation
- Sudden change detection
- indoor_01 vs indoor_02 comparison
- device_fault status check
- internal voltage monitoring for device health

### Step 5. AI-based Anomaly Analysis (Optional)
Unsupervised anomaly scoring model (e.g., Isolation Forest) calculates anomaly scores.

### Step 6. Final Payload Build
Combine:
- raw sensor data
- rule-based anomaly results
- AI anomaly score
- metadata (`site_id`, `device_id`, `timestamp`)

### Step 7. oneM2M Conversion
The final payload is wrapped into oneM2M format before transmission.

### Step 8. Server Upload
The converted payload is transmitted to the server.

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
Compare indoor_01 and indoor_02:
- temperature difference
- humidity difference
- co2 difference

#### Device Status
- `device_fault == true`
- internal voltage low condition

### 2nd Stage: AI-based Detection
AI-based anomaly score using models such as Isolation Forest.

Used for:
- long-term drift detection
- abnormal pattern detection
- rule-based decision support

## Example Final Payload Before oneM2M Conversion
```json
{
  "site_id": "site_01",
  "device_id": "indoor_01",
  "timestamp": "2026-03-10T12:10:21",
  "data": {
    "temperature": 24.6,
    "humidity": 63.2,
    "co2": 512,
    "device_fault": false
  },
  "anomaly": {
    "rule_status": "normal",
    "rule_flags": [],
    "ai_score": 0.12,
    "ai_status": "normal"
  }
}
```

## Site ID Rules
```text
site_01
site_02
site_03
site_04
site_05
site_06
site_07
site_08
```

## Device ID Rules
```text
indoor_01
indoor_02
outdoor_01
```

### Final Identification Rule
```text
site_id + device_id
```

### Example
```text
site_01 / indoor_01
site_01 / indoor_02
site_01 / outdoor_01

site_02 / indoor_01
site_02 / indoor_02
site_02 / outdoor_01
```

## TODO
- [ ] implement indoor/outdoor firmware
- [ ] implement Raspberry Pi MQTT collector
- [ ] implement data validation module
- [ ] define rule-based anomaly detection thresholds
- [ ] implement AI anomaly scoring module
- [ ] finalize oneM2M payload format
- [ ] implement server upload interface
- [x] register systemd service