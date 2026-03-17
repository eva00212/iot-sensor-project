/*
 * indoor_node.ino
 * SmartFarm Indoor Sensor Node
 *
 * Board  : Arduino UNO R4 WiFi
 * Sensors: SHT40    (temperature, humidity)       – I2C
 *          INA3221  ch1 (voltage – internal fault detection only) – I2C
 *          CM1106   (co2, UART Modbus)             – Serial1 D0/D1
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
#define DEVICE_ID "indoor_01"

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

// ── Sensor Config ─────────────────────────────────────────────────────────────
#define INA3221_ADDR    0x40
#define INA3221_CHANNEL 1
#define VOLTAGE_MIN     4.5f   // below this threshold → device_fault

// ── CM1106 CO2 Config ─────────────────────────────────────────────────────────
#define CM1106_BAUD        9600
#define CM1106_TIMEOUT_MS  1000

// ── Publish Interval ─────────────────────────────────────────────────────────
const unsigned long PUBLISH_INTERVAL_MS = 30000UL;

// ── MQTT / WiFi clients ───────────────────────────────────────────────────────
WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);

// ── Sensors ───────────────────────────────────────────────────────────────────
Adafruit_SHT4x sht4;
INA3221        ina3221(INA3221_ADDR);

bool sht40_ok  = false;
bool ina_ok    = false;

// ── State ─────────────────────────────────────────────────────────────────────
unsigned long lastPublish = 0;

// ── CM1106 CO2 Reader ─────────────────────────────────────────────────────────
// Returns CO2 in ppm, or -1 on failure.
// Protocol: send 4-byte command, receive 7-byte response over Serial1.
int readCO2() {
    while (Serial1.available()) Serial1.read();  // flush

    uint8_t cmd[4] = {0x11, 0x01, 0x01, 0xED};
    Serial1.write(cmd, 4);
    Serial1.flush();

    unsigned long start = millis();
    while (Serial1.available() < 7) {
        if (millis() - start > CM1106_TIMEOUT_MS) {
            Serial.println("[CM1106] timeout");
            return -1;
        }
    }

    uint8_t resp[7];
    Serial1.readBytes(resp, 7);

    if (resp[0] != 0x16 || resp[1] != 0x05 || resp[2] != 0x01) {
        Serial.println("[CM1106] invalid response");
        return -1;
    }

    uint8_t cs = (0x100 - ((resp[1] + resp[2] + resp[3] + resp[4] + resp[5]) & 0xFF)) & 0xFF;
    if (resp[6] != cs) {
        Serial.println("[CM1106] checksum error");
        return -1;
    }

    return ((uint16_t)resp[3] << 8) | resp[4];
}

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

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    Wire.begin();
    Serial1.begin(CM1106_BAUD);
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

        bool  fault       = false;
        float temperature = 0.0f;
        float humidity    = 0.0f;
        int   co2         = -1;  // -1 = not available

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

        // Read CM1106 CO2
        int co2_raw = readCO2();
        if (co2_raw < 0) {
            fault = true;
        } else {
            co2 = co2_raw;
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

        // Build and publish JSON payload
        // timestamp is omitted — Raspberry Pi injects it on receipt
        StaticJsonDocument<256> doc;
        doc["site_id"]      = siteId;
        doc["device_id"]    = DEVICE_ID;
        doc["temperature"]  = (float)(round(temperature * 10) / 10.0);
        doc["humidity"]     = (float)(round(humidity    * 10) / 10.0);
        if (co2 >= 0) doc["co2"] = co2;
        doc["device_fault"] = fault;

        char payload[256];
        serializeJson(doc, payload);

        if (mqttClient.publish(mqttTopic, payload)) {
            Serial.print("[MQTT] published: ");
            Serial.println(payload);
        } else {
            Serial.println("[MQTT] publish failed");
        }
    }
}
