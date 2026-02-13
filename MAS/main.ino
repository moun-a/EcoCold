/************ LIBRARIES ************/
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <DHT.h>

/************ MQTT ************/
const char* mqtt_server = "178.***.***.***";
const int   mqtt_port   = 443;
const char* mqtt_topic  = "echocold/fridge_01";

/************ PINS ************/
#define DHTPIN 4
#define DHTTYPE DHT11
#define MIC_PIN 34

/************ OBJECTS ************/
WebServer server(80);
Preferences prefs;
WiFiClient espClient;
PubSubClient client(espClient);
DHT dht(DHTPIN, DHTTYPE);
Adafruit_MPU6050 mpu;

/************ GLOBALS ************/
bool wifiConnected = false;

/************ WIFI CONNECT ************/
bool connectWiFi() {
  prefs.begin("wifi", true);
  String ssid = prefs.getString("ssid", "");
  String pass = prefs.getString("pass", "");
  prefs.end();

  if (ssid.length() == 0) return false;

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(ssid.c_str(), pass.c_str());

  Serial.print("Connecting to ");
  Serial.println(ssid);

  unsigned long start = millis();
  while (millis() - start < 15000) {
    if (WiFi.status() == WL_CONNECTED) {
      Serial.print("Connected. IP: ");
      Serial.println(WiFi.localIP());
      return true;
    }
    delay(300);
    Serial.print(".");
  }

  Serial.println("\nWiFi failed");
  return false;
}

/************ AP MODE ************/
void startAP() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP("EchoCold_Setup");

  Serial.print("AP IP: ");
  Serial.println(WiFi.softAPIP());

  server.on("/", []() {
    server.send(200, "text/html",
      "<h2>EchoCold WiFi Setup</h2>"
      "<form action='/save'>"
      "SSID:<input name='s'><br>"
      "Password:<input name='p' type='password'><br><br>"
      "<input type='submit' value='Save'>"
      "</form>");
  });

  server.on("/save", []() {
    prefs.begin("wifi", false);
    prefs.putString("ssid", server.arg("s"));
    prefs.putString("pass", server.arg("p"));
    prefs.end();

    server.send(200, "text/html", "Saved. Rebooting...");
    delay(1000);
    ESP.restart();
  });

  server.begin();
}

/************ MQTT RECONNECT ************/
void reconnectMQTT() {
  while (!client.connected()) {
    Serial.print("Connecting to MQTT...");
    String cid = "EchoCold-" + String(random(0xffff), HEX);
    if (client.connect(cid.c_str())) {
      Serial.println("OK");
    } else {
      Serial.print("Fail rc=");
      Serial.println(client.state());
      delay(3000);
    }
  }
}

/************ SETUP ************/
void setup() {
  Serial.begin(115200);
  delay(500);

  wifiConnected = connectWiFi();
  if (!wifiConnected) startAP();

  client.setServer(mqtt_server, mqtt_port);

  dht.begin();
  Wire.begin(21, 22);
  pinMode(MIC_PIN, INPUT);

  if (!mpu.begin()) {
    Serial.println("MPU6050 not found");
    while (1);
  }

  mpu.setAccelerometerRange(MPU6050_RANGE_2_G);
  mpu.setGyroRange(MPU6050_RANGE_250_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
}

/************ LOOP ************/
void loop() {

  // AP MODE
  if (WiFi.getMode() == WIFI_AP) {
    server.handleClient();
    return;
  }

  // WIFI LOST
  if (WiFi.status() != WL_CONNECTED) return;

  // MQTT
  if (!client.connected()) reconnectMQTT();
  client.loop();

  // SENSOR READ
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (isnan(t)) t = 0;

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  float raw_force = sqrt(
    a.acceleration.x * a.acceleration.x +
    a.acceleration.y * a.acceleration.y +
    a.acceleration.z * a.acceleration.z
  );

  float vib = abs(raw_force - 9.80);
  if (vib < 1.5) vib = 0.0;

  int mic = analogRead(MIC_PIN);

  String payload = "{";
  payload += "\"temp\":" + String(t,1) + ",";
  payload += "\"hum\":"  + String(h,1) + ",";
  payload += "\"vib\":"  + String(vib,2) + ",";
  payload += "\"mic\":"  + String(mic);
  payload += "}";

  Serial.println(payload);
  client.publish(mqtt_topic, payload.c_str());

  delay(3000);
}
