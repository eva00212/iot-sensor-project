/*
 * outdoor_node.ino
 * SmartFarm Outdoor Sensor Node
 *
 * Board  : Arduino UNO R4 WiFi
 * Sensors: SHT40        (temperature, humidity)       – I2C
 *          INA3221 ch1  (voltage – internal fault detection only) – I2C
 *          NS-RSRM      (rain_detected)                – Digital D2
 *          SEN0640      (solar_radiation, RS485 Modbus RTU) – Serial1
 *          SEN0170      (wind_speed) – TODO: not connected yet
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
#define DEVICE_ID "outdoor_01"

// ── WiFi Config ───────────────────────────────────────────────────────────────
const char* WIFI_SSID     = "area 1";
const char* WIFI_PASSWORD = "00000000";

// ── MQTT Config ───────────────────────────────────────────────────────────────
const char* MQTT_BROKER = "192.168.0.10";  // Raspberry Pi local IP
const int   MQTT_PORT   = 1883;
const char* MQTT_TOPIC  = "smartfarm/" SITE_ID "/" DEVICE_ID "/raw";

// ── Pin Config ────────────────────────────────────────────────────────────────
#define RAIN_PIN 2   // NS-RSRM OUT → D2

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

// ── NTP (UTC+9, KST) ─────────────────────────────────────────────────────────
WiFiUDP   ntpUDP;
NTPClient timeClient(ntpUDP, "pool.ntp.org", 9 * 3600L);

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

    Serial.println("[RS485] Serial1 ready");
    Serial.println("[RAIN] pin ready");
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

        bool  fault          = false;
        float temperature    = 0.0f;
        float humidity       = 0.0f;
        bool  rain_detected  = false;
        float solar_radiation = 0.0f;

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

        // Read solar radiation via RS485 Modbus
        float solar = readSolarRadiation();
        if (solar < 0.0f) {
            Serial.println("[SEN0640] read failed");
            fault = true;
            solar_radiation = 0.0f;
        } else {
            solar_radiation = solar;
        }

        // Build and publish JSON payload
        // wind_speed omitted — SEN0170 not connected yet
        timeClient.update();
        StaticJsonDocument<300> doc;
        doc["site_id"]         = SITE_ID;
        doc["device_id"]       = DEVICE_ID;
        doc["timestamp"]       = epochToISO8601(timeClient.getEpochTime());
        doc["temperature"]     = (float)(round(temperature     * 10) / 10.0);
        doc["humidity"]        = (float)(round(humidity        * 10) / 10.0);
        doc["rain_detected"]   = rain_detected;
        doc["solar_radiation"] = (float)(round(solar_radiation * 10) / 10.0);
        doc["device_fault"]    = fault;

        char payload[300];
        serializeJson(doc, payload);

        if (mqttClient.publish(MQTT_TOPIC, payload)) {
            Serial.print("[MQTT] published: ");
            Serial.println(payload);
        } else {
            Serial.println("[MQTT] publish failed");
        }
    }
}
