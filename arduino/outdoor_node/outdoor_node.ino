/*
 * outdoor_node.ino
 * SmartFarm Outdoor Sensor Node
 *
 * Board  : Arduino UNO R4 WiFi
 * Sensors: SHT40        (temperature, humidity)       – I2C
 *          INA3221 ch1  (voltage – internal fault detection only) – I2C
 *          NS-RSRM      (rain_detected)                – Digital D2
 *          SEN0640      (solar_radiation, RS485 Modbus RTU) – Serial1
 *          SEN0170      (wind_speed, analog A0)
 *
 * Libraries (install via Library Manager):
 *   - Adafruit SHT4x Library  (+ Adafruit BusIO)
 *   - INA3221 by Rob Tillaart
 *   - PubSubClient by Nick O'Leary
 *   - ArduinoJson by Benoit Blanchon
 *
 * Site ID configuration (Serial commands):
 *   SET_SITE:site_XX  – save site ID to EEPROM and restart
 *   GET_SITE          – print current site ID
 */

#include <Wire.h>
#include <WiFiS3.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "Adafruit_SHT4x.h"
#include <INA3221.h>
#include <EEPROM.h>

// ── Device Config (fixed per firmware) ───────────────────────────────────────
#define DEVICE_ID "outdoor_01"

// ── EEPROM Layout ─────────────────────────────────────────────────────────────
#define EEPROM_MAGIC_ADDR  0          // 1 byte  – 0xAB if initialized
#define EEPROM_SITE_ADDR   1          // 16 bytes – site_id string
#define EEPROM_MAGIC_VAL   0xAB
#define SITE_ID_MAX_LEN    16

// ── Site ID (loaded from EEPROM at boot) ──────────────────────────────────────
char siteId[SITE_ID_MAX_LEN] = "";

// ── WiFi Config ───────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "area1";
const char* WIFI_PASSWORD = "00000000";

// ── MQTT Config ───────────────────────────────────────────────────────────────
const char* MQTT_BROKER = "smartfarm.local";
const int   MQTT_PORT   = 1883;
char        mqttTopic[64] = "";

// ── Pin Config ────────────────────────────────────────────────────────────────
#define RAIN_PIN 2   // NS-RSRM OUT → D2
#define WIND_PIN A0  // SEN0170 OUT → A0

// ── Sensor Config ─────────────────────────────────────────────────────────────
#define INA3221_ADDR    0x40
#define INA3221_CHANNEL 1
#define VOLTAGE_MIN     4.5f

// ── RS485 / SEN0640 Config ────────────────────────────────────────────────────
#define RS485_BAUD         9600
#define MODBUS_SLAVE_ADDR  0x01
#define MODBUS_TIMEOUT_MS  500

// ── Publish Interval ─────────────────────────────────────────────────────────
const unsigned long PUBLISH_INTERVAL_MS = 30000UL;

// ── MQTT / WiFi clients ───────────────────────────────────────────────────────
WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);

// ── Sensors ───────────────────────────────────────────────────────────────────
Adafruit_SHT4x sht4;
INA3221        ina3221(INA3221_ADDR);

bool sht40_ok = false;
bool ina_ok   = false;

// ── State ─────────────────────────────────────────────────────────────────────
unsigned long lastPublish = 0;

// ── EEPROM Helpers ────────────────────────────────────────────────────────────
void loadSiteId() {
    if (EEPROM.read(EEPROM_MAGIC_ADDR) != EEPROM_MAGIC_VAL) return;
    for (int i = 0; i < SITE_ID_MAX_LEN; i++) {
        siteId[i] = EEPROM.read(EEPROM_SITE_ADDR + i);
        if (siteId[i] == '\0') break;
    }
    siteId[SITE_ID_MAX_LEN - 1] = '\0';
}

void saveSiteId(const char* id) {
    EEPROM.write(EEPROM_MAGIC_ADDR, EEPROM_MAGIC_VAL);
    for (int i = 0; i < SITE_ID_MAX_LEN; i++) {
        EEPROM.write(EEPROM_SITE_ADDR + i, id[i]);
        if (id[i] == '\0') break;
    }
}

// ── Serial Config Handler ─────────────────────────────────────────────────────
// Commands: SET_SITE:site_XX  |  GET_SITE
void handleSerialConfig() {
    if (!Serial.available()) return;
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.startsWith("SET_SITE:")) {
        String id = cmd.substring(9);
        id.trim();
        if (id.length() == 0 || id.length() >= SITE_ID_MAX_LEN) {
            Serial.println("[CONFIG] Invalid site ID");
            return;
        }
        id.toCharArray(siteId, SITE_ID_MAX_LEN);
        saveSiteId(siteId);
        Serial.print("[CONFIG] Site ID saved: ");
        Serial.println(siteId);
        Serial.println("[CONFIG] Restarting...");
        delay(100);
        NVIC_SystemReset();
    } else if (cmd == "GET_SITE") {
        Serial.print("[CONFIG] Site ID: ");
        Serial.println(strlen(siteId) > 0 ? siteId : "(not set)");
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
void connectWifi() {
    Serial.print("[WiFi] Connecting");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    while (WiFi.localIP().toString() == "0.0.0.0") {
        delay(500);
        Serial.print(".");
    }
    Serial.print("\n[WiFi] Connected: ");
    Serial.println(WiFi.localIP());
}

void connectMqtt() {
    Serial.print("[MQTT] Connecting...");
    if (mqttClient.connect(DEVICE_ID)) {
        Serial.println("connected");
    } else {
        Serial.print("failed rc=");
        Serial.print(mqttClient.state());
        Serial.println(", retry in 5s");
    }
}

// ── Modbus CRC16 ──────────────────────────────────────────────────────────────
uint16_t crc16(uint8_t* buf, uint16_t len) {
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= buf[i];
        for (uint8_t j = 0; j < 8; j++) {
            crc = (crc & 0x0001) ? (crc >> 1) ^ 0xA001 : crc >> 1;
        }
    }
    return crc;
}

// ── SEN0170 Wind Speed (Analog) ───────────────────────────────────────────────
// Output: 0.4V (0 m/s) ~ 2.0V (32.4 m/s), ADC ref 3.3V, 10-bit
// Returns wind speed in m/s (>= 0)
float readWindSpeed() {
    int   raw     = analogRead(WIND_PIN);
    float voltage = raw * (3.3f / 1023.0f);
    float speed   = (voltage - 0.4f) * (32.4f / 1.6f);
    return max(speed, 0.0f);
}

// ── SEN0640 Solar Radiation (Modbus RTU over RS485) ───────────────────────────
// Returns solar radiation in W/m², or -1.0 on failure
float readSolarRadiation() {
    // Build Modbus RTU read request: slave 0x01, func 0x03, reg 0x0000, count 0x0001
    uint8_t req[6] = {MODBUS_SLAVE_ADDR, 0x03, 0x00, 0x00, 0x00, 0x01};
    uint16_t crc = crc16(req, 6);
    uint8_t frame[8];
    memcpy(frame, req, 6);
    frame[6] = crc & 0xFF;         // CRC low byte first (Modbus)
    frame[7] = (crc >> 8) & 0xFF;

    // Flush and send
    while (Serial1.available()) Serial1.read();
    Serial1.write(frame, 8);
    Serial1.flush();

    // Wait for response (7 bytes: addr + func + bytecount + 2 data + 2 CRC)
    unsigned long start = millis();
    while (Serial1.available() < 7) {
        if (millis() - start > MODBUS_TIMEOUT_MS) {
            Serial.println("[SEN0640] timeout");
            return -1.0f;
        }
    }

    uint8_t resp[7];
    Serial1.readBytes(resp, 7);

    // Validate CRC
    uint16_t respCrc = crc16(resp, 5);
    if ((resp[5] != (respCrc & 0xFF)) || (resp[6] != (respCrc >> 8))) {
        Serial.println("[SEN0640] CRC error");
        return -1.0f;
    }

    // Validate slave address and function code
    if (resp[0] != MODBUS_SLAVE_ADDR || resp[1] != 0x03 || resp[2] != 0x02) {
        Serial.println("[SEN0640] invalid response");
        return -1.0f;
    }

    uint16_t raw = ((uint16_t)resp[3] << 8) | resp[4];
    return (float)raw;  // W/m²
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    Wire.begin();
    Serial1.begin(RS485_BAUD);
    pinMode(RAIN_PIN, INPUT);
    delay(2000);

    // Load site ID from EEPROM; wait for Serial config if not set
    loadSiteId();
    if (strlen(siteId) == 0) {
        Serial.println("[CONFIG] No site ID set. Send: SET_SITE:site_XX");
        while (strlen(siteId) == 0) {
            handleSerialConfig();
            delay(100);
        }
    }
    snprintf(mqttTopic, sizeof(mqttTopic), "smartfarm/%s/%s/raw", siteId, DEVICE_ID);
    Serial.print("[CONFIG] Site ID: ");
    Serial.println(siteId);
    Serial.print("[CONFIG] Topic: ");
    Serial.println(mqttTopic);

    connectWifi();

    mqttClient.setBufferSize(512);
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);

    sht40_ok = sht4.begin();
    if (sht40_ok) {
        sht4.setPrecision(SHT4X_HIGH_PRECISION);
        Serial.println("[SHT40] ready");
    } else {
        Serial.println("[SHT40] not found");
    }

    ina_ok = ina3221.begin();
    if (ina_ok) {
        Serial.println("[INA3221] ready");
    } else {
        Serial.println("[INA3221] not found");
    }

    Serial.println("[RS485] Serial1 ready");
    Serial.println("[RAIN] pin ready");
    Serial.println("[SEN0170] analog pin ready");
}

// ── Loop ──────────────────────────────────────────────────────────────────────
void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WiFi] lost, reconnecting...");
        connectWifi();
    }

    if (!mqttClient.connected()) {
        static unsigned long lastReconnectAttempt = 0;
        if (millis() - lastReconnectAttempt >= 5000) {
            lastReconnectAttempt = millis();
            connectMqtt();
        }
        return;
    }
    mqttClient.loop();
    handleSerialConfig();

    if (millis() - lastPublish >= PUBLISH_INTERVAL_MS) {
        lastPublish = millis();

        bool  fault           = false;
        float temperature     = 0.0f;
        float humidity        = 0.0f;
        bool  rain_detected   = false;
        float wind_speed      = 0.0f;
        float solar_radiation = -1.0f;  // -1.0 = not available

        // Read SHT40
        if (sht40_ok) {
            sensors_event_t hum_evt, temp_evt;
            if (sht4.getEvent(&hum_evt, &temp_evt)) {
                temperature = temp_evt.temperature;
                humidity    = hum_evt.relative_humidity;
            } else {
                Serial.println("[SHT40] read failed");
                fault = true;
            }
        } else {
            fault = true;
        }

        // Read INA3221 voltage (internal use only)
        if (ina_ok) {
            float voltage = ina3221.getBusVoltage(INA3221_CHANNEL);
            if (voltage < VOLTAGE_MIN) {
                Serial.print("[INA3221] low voltage: ");
                Serial.println(voltage);
                fault = true;
            }
        } else {
            fault = true;
        }

        // Read rain sensor (LOW = rain detected)
        rain_detected = (digitalRead(RAIN_PIN) == LOW);

        // Read wind speed via analog
        wind_speed = readWindSpeed();

        // Read solar radiation via RS485 Modbus
        float solar = readSolarRadiation();
        if (solar < 0.0f) {
            Serial.println("[SEN0640] read failed");
            fault = true;
        } else {
            solar_radiation = solar;
        }

        // Build and publish JSON payload
        // timestamp omitted — Raspberry Pi injects it on receipt
        StaticJsonDocument<300> doc;
        doc["site_id"]         = siteId;
        doc["device_id"]       = DEVICE_ID;
        doc["temperature"]     = (float)(round(temperature     * 10) / 10.0);
        doc["humidity"]        = (float)(round(humidity        * 10) / 10.0);
        doc["wind_speed"]      = (float)(round(wind_speed      * 10) / 10.0);
        doc["rain_detected"]   = rain_detected;
        if (solar_radiation >= 0.0f)
            doc["solar_radiation"] = (float)(round(solar_radiation * 10) / 10.0);
        doc["device_fault"]    = fault;

        char payload[300];
        serializeJson(doc, payload);

        if (mqttClient.publish(mqttTopic, payload)) {
            Serial.print("[MQTT] published: ");
            Serial.println(payload);
        } else {
            Serial.println("[MQTT] publish failed");
        }
    }
}
