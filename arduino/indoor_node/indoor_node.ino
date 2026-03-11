/*
 * indoor_node.ino
 * SmartFarm Indoor Sensor Node
 *
 * Board  : Arduino UNO R4 WiFi
 * Sensors: SHT40 (temperature, humidity)
 *          INA3221 ch1 (voltage – internal fault detection only)
 *
 * Libraries (install via Library Manager):
 *   - Adafruit SHT4x Library  (+ Adafruit BusIO)
 *   - INA3221 by Rob Tillaart
 *   - PubSubClient by Nick O'Leary
 *   - ArduinoJson by Benoit Blanchon
 *   - NTPClient by Fabrice Weinberg
 */

#include <Wire.h>
#include <WiFiS3.h>
#include <WiFiUdp.h>
#include <NTPClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "Adafruit_SHT4x.h"
#include <INA3221.h>

// ── Site / Device Config ──────────────────────────────────────────────────────
#define SITE_ID   "site_01"
#define DEVICE_ID "indoor_01"

// ── WiFi Config ───────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "your_ssid";
const char* WIFI_PASSWORD = "your_password";

// ── MQTT Config ───────────────────────────────────────────────────────────────
const char* MQTT_BROKER = "192.168.1.100";  // Raspberry Pi local IP
const int   MQTT_PORT   = 1883;
const char* MQTT_TOPIC  = "smartfarm/" SITE_ID "/" DEVICE_ID "/raw";

// ── Sensor Config ─────────────────────────────────────────────────────────────
#define INA3221_ADDR    0x40
#define INA3221_CHANNEL 1
#define VOLTAGE_MIN     4.5f   // below this threshold → device_fault

// ── Publish Interval ─────────────────────────────────────────────────────────
const unsigned long PUBLISH_INTERVAL_MS = 30000UL;

// ── NTP (UTC+9, KST) ─────────────────────────────────────────────────────────
WiFiUDP   ntpUDP;
NTPClient timeClient(ntpUDP, "pool.ntp.org", 9 * 3600L);

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

// ── Helpers ───────────────────────────────────────────────────────────────────
void connectWifi() {
    Serial.print("[WiFi] Connecting");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.print("\n[WiFi] Connected: ");
    Serial.println(WiFi.localIP());
}

void connectMqtt() {
    while (!mqttClient.connected()) {
        Serial.print("[MQTT] Connecting...");
        if (mqttClient.connect(DEVICE_ID)) {
            Serial.println("connected");
        } else {
            Serial.print("failed rc=");
            Serial.print(mqttClient.state());
            Serial.println(", retry in 5s");
            delay(5000);
        }
    }
}

// Converts NTP epoch (already offset by UTC+9) to ISO 8601 string
String epochToISO8601(unsigned long epoch) {
    const uint8_t daysInMonth[] = {31,28,31,30,31,30,31,31,30,31,30,31};

    uint32_t days      = epoch / 86400UL;
    uint32_t timeOfDay = epoch % 86400UL;
    uint8_t  h = timeOfDay / 3600;
    uint8_t  m = (timeOfDay % 3600) / 60;
    uint8_t  s = timeOfDay % 60;

    uint16_t year = 1970;
    while (true) {
        bool leap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
        uint16_t diy = leap ? 366 : 365;
        if (days < diy) break;
        days -= diy;
        year++;
    }

    bool    leap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
    uint8_t month = 1;
    for (int i = 0; i < 12; i++) {
        uint8_t dim = daysInMonth[i] + (i == 1 && leap ? 1 : 0);
        if (days < dim) break;
        days -= dim;
        month++;
    }
    uint8_t day = days + 1;

    char buf[20];
    sprintf(buf, "%04d-%02d-%02dT%02d:%02d:%02d", year, month, day, h, m, s);
    return String(buf);
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    Wire.begin();
    delay(2000);

    connectWifi();

    timeClient.begin();
    timeClient.update();

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
        connectMqtt();
    }
    mqttClient.loop();

    if (millis() - lastPublish >= PUBLISH_INTERVAL_MS) {
        lastPublish = millis();

        bool  fault       = false;
        float temperature = 0.0f;
        float humidity    = 0.0f;

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

        // Build and publish JSON payload
        timeClient.update();
        StaticJsonDocument<256> doc;
        doc["site_id"]      = SITE_ID;
        doc["device_id"]    = DEVICE_ID;
        doc["timestamp"]    = epochToISO8601(timeClient.getEpochTime());
        doc["temperature"]  = (float)(round(temperature * 10) / 10.0);
        doc["humidity"]     = (float)(round(humidity    * 10) / 10.0);
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
