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
 */

#include <Wire.h>
#include <WiFiS3.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "Adafruit_SHT4x.h"
#include <INA3221.h>

// ── Site / Device Config ──────────────────────────────────────────────────────
#define SITE_ID   "site_01"
#define DEVICE_ID "indoor_01"

// ── WiFi Config ───────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "area1";
const char* WIFI_PASSWORD = "00000000";

// ── MQTT Config ───────────────────────────────────────────────────────────────
const char* MQTT_BROKER = "192.168.0.10";  // Raspberry Pi local IP
const int   MQTT_PORT   = 1883;
const char* MQTT_TOPIC  = "smartfarm/" SITE_ID "/" DEVICE_ID "/raw";

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
        doc["site_id"]      = SITE_ID;
        doc["device_id"]    = DEVICE_ID;
        doc["temperature"]  = (float)(round(temperature * 10) / 10.0);
        doc["humidity"]     = (float)(round(humidity    * 10) / 10.0);
        if (co2 >= 0) doc["co2"] = co2;
        doc["device_fault"] = fault;

        char payload[256];
        serializeJson(doc, payload);

        if (mqttClient.publish(MQTT_TOPIC, payload)) {
            Serial.print("[MQTT] published: ");
            Serial.println(payload);
        } else {
            Serial.println("[MQTT] publish failed");
        }
    }
}
