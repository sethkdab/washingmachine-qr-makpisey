#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

const char* WIFI_SSID = "Vatey";
const char* WIFI_PASSWORD = "Piseth@2001";

const char* BASE_URL = "http://192.168.100.88:5000";
const char* MACHINE_CODE = "WM-01";
const char* MACHINE_TOKEN = "dev_96e266255cac4f55";

const int POWER_BUTTON_PIN = 2;
const int START_PAUSE_BUTTON_PIN = 1;
const int KNOB_PIN_1 = 4;
const int KNOB_PIN_2 = 3;

const bool BUTTON_IDLE_HIGH = true;
const unsigned long POWER_HOLD_MS = 2000;
const unsigned long START_PRESS_MS = 1000;
const unsigned long DOOR_HOLD_MS = 3000;
const unsigned long BUTTON_SETTLE_MS = 250;
const unsigned long KNOB_STEP_DELAY_MS = 120;

const unsigned long POLL_INTERVAL_MS = 2500;
const unsigned long HEARTBEAT_INTERVAL_MS = 10000;
const unsigned long FINISH_RETRY_INTERVAL_MS = 5000;
const unsigned long OVERRIDE_RUN_DURATION_MS = 0;

String lastExecutedCommandId = "";
String activeCommandId = "";
String activeSessionId = "";
bool machineRunning = false;
bool finishPending = false;

unsigned long runStartedAtMs = 0;
unsigned long runDurationMs = 0;
unsigned long lastPollAtMs = 0;
unsigned long lastHeartbeatAtMs = 0;
unsigned long lastFinishAttemptAtMs = 0;

void setButtonIdle(int pin) {
  digitalWrite(pin, BUTTON_IDLE_HIGH ? HIGH : LOW);
}

void pressButtonFor(int pin, const char* label, unsigned long holdMs) {
  Serial.printf("[control] Holding %s button for %lu ms\n", label, holdMs);
  digitalWrite(pin, BUTTON_IDLE_HIGH ? LOW : HIGH);
  delay(holdMs);
  setButtonIdle(pin);
  delay(BUTTON_SETTLE_MS);
}

void setKnobIdle() {
  digitalWrite(KNOB_PIN_1, HIGH);
  digitalWrite(KNOB_PIN_2, HIGH);
}

void knobClockwiseStep() {
  Serial.println("[control] Knob clockwise step");
  digitalWrite(KNOB_PIN_1, LOW);
  delay(KNOB_STEP_DELAY_MS);
  digitalWrite(KNOB_PIN_2, LOW);
  delay(KNOB_STEP_DELAY_MS);
  digitalWrite(KNOB_PIN_1, HIGH);
  delay(KNOB_STEP_DELAY_MS);
  digitalWrite(KNOB_PIN_2, HIGH);
  delay(KNOB_STEP_DELAY_MS);
}

void knobCounterClockwiseStep() {
  Serial.println("[control] Knob counterclockwise step");
  digitalWrite(KNOB_PIN_2, LOW);
  delay(KNOB_STEP_DELAY_MS);
  digitalWrite(KNOB_PIN_1, LOW);
  delay(KNOB_STEP_DELAY_MS);
  digitalWrite(KNOB_PIN_2, HIGH);
  delay(KNOB_STEP_DELAY_MS);
  digitalWrite(KNOB_PIN_1, HIGH);
  delay(KNOB_STEP_DELAY_MS);
}

void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[wifi] Connecting");

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("[wifi] Connected");
  Serial.print("[wifi] IP: ");
  Serial.println(WiFi.localIP());
}

bool httpPostJson(const String& url, const String& body, String& responseOut) {
  HTTPClient http;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Machine-Token", MACHINE_TOKEN);

  int httpCode = http.POST(body);
  responseOut = http.getString();

  Serial.printf("[http] POST %s -> %d\n", url.c_str(), httpCode);
  if (httpCode >= 0) {
    Serial.println(responseOut);
  } else {
    Serial.printf("[http] POST failed: %s\n", http.errorToString(httpCode).c_str());
  }

  http.end();
  return httpCode >= 200 && httpCode < 300;
}

bool httpGet(const String& url, String& responseOut) {
  HTTPClient http;
  http.begin(url);
  http.addHeader("X-Machine-Token", MACHINE_TOKEN);

  int httpCode = http.GET();
  responseOut = http.getString();

  Serial.printf("[http] GET %s -> %d\n", url.c_str(), httpCode);
  if (httpCode >= 0) {
    Serial.println(responseOut);
  } else {
    Serial.printf("[http] GET failed: %s\n", http.errorToString(httpCode).c_str());
  }

  http.end();
  return httpCode >= 200 && httpCode < 300;
}

void sendHeartbeat() {
  String response;
  String url = String(BASE_URL) + "/esp32/" + MACHINE_CODE + "/heartbeat";
  httpPostJson(url, "{}", response);
}

bool ackCommand(const String& dbCommandId, const String& sessionId) {
  String response;
  String url = String(BASE_URL) + "/esp32/" + MACHINE_CODE + "/ack";

  StaticJsonDocument<256> doc;
  doc["db_command_id"] = dbCommandId;
  doc["session_id"] = sessionId;

  String body;
  serializeJson(doc, body);
  return httpPostJson(url, body, response);
}

bool reportCommandDone(const String& dbCommandId) {
  String response;
  String url = String(BASE_URL) + "/esp32/" + MACHINE_CODE + "/command-done";

  StaticJsonDocument<192> doc;
  doc["db_command_id"] = dbCommandId;

  String body;
  serializeJson(doc, body);
  return httpPostJson(url, body, response);
}

bool reportFinished(const String& dbCommandId, const String& sessionId) {
  String response;
  String url = String(BASE_URL) + "/esp32/" + MACHINE_CODE + "/finished";

  StaticJsonDocument<256> doc;
  doc["db_command_id"] = dbCommandId;
  doc["session_id"] = sessionId;

  String body;
  serializeJson(doc, body);
  return httpPostJson(url, body, response);
}

void startMachineRun(const String& dbCommandId, const String& sessionId, int durationMinutes) {
  activeCommandId = dbCommandId;
  activeSessionId = sessionId;
  machineRunning = true;
  finishPending = false;
  runStartedAtMs = millis();
  runDurationMs = OVERRIDE_RUN_DURATION_MS > 0
    ? OVERRIDE_RUN_DURATION_MS
    : (unsigned long) durationMinutes * 60UL * 1000UL;

  pressButtonFor(POWER_BUTTON_PIN, "power", POWER_HOLD_MS);
  pressButtonFor(START_PAUSE_BUTTON_PIN, "start/pause", START_PRESS_MS);

  Serial.println("[machine] Paid wash start sequence sent: power hold then start press");
  Serial.printf("[machine] session=%s command=%s durationMs=%lu\n", activeSessionId.c_str(), activeCommandId.c_str(), runDurationMs);
}

void stopMachineRun() {
  machineRunning = false;
  runStartedAtMs = 0;
  runDurationMs = 0;
  finishPending = true;
  lastFinishAttemptAtMs = 0;
  Serial.println("[machine] Paid run timer completed");
}

void maybeSendFinished() {
  if (!finishPending || activeSessionId.isEmpty() || activeCommandId.isEmpty()) {
    return;
  }

  unsigned long nowMs = millis();
  if (lastFinishAttemptAtMs != 0 && nowMs - lastFinishAttemptAtMs < FINISH_RETRY_INTERVAL_MS) {
    return;
  }
  lastFinishAttemptAtMs = nowMs;

  Serial.printf("[machine] Reporting finished session=%s command=%s\n", activeSessionId.c_str(), activeCommandId.c_str());
  if (reportFinished(activeCommandId, activeSessionId)) {
    Serial.println("[machine] Finished reported successfully");
    lastExecutedCommandId = activeCommandId;
    activeCommandId = "";
    activeSessionId = "";
    finishPending = false;
  } else {
    Serial.println("[machine] Finished report failed, will retry");
  }
}

void executeManualCommand(const String& dbCommandId, const String& type, int steps) {
  if (type == "POWER_HOLD") {
    pressButtonFor(POWER_BUTTON_PIN, "power", POWER_HOLD_MS);
  } else if (type == "START_PAUSE_HOLD") {
    pressButtonFor(START_PAUSE_BUTTON_PIN, "start/pause", DOOR_HOLD_MS);
  } else if (type == "KNOB_CLOCKWISE") {
    for (int i = 0; i < steps; i++) {
      knobClockwiseStep();
    }
  } else if (type == "KNOB_COUNTERCLOCKWISE") {
    for (int i = 0; i < steps; i++) {
      knobCounterClockwiseStep();
    }
  } else {
    Serial.printf("[control] Unknown manual command type=%s\n", type.c_str());
    return;
  }

  setKnobIdle();
  setButtonIdle(POWER_BUTTON_PIN);
  setButtonIdle(START_PAUSE_BUTTON_PIN);

  if (reportCommandDone(dbCommandId)) {
    Serial.printf("[control] Manual command completed db_command_id=%s\n", dbCommandId.c_str());
    lastExecutedCommandId = dbCommandId;
  } else {
    Serial.printf("[control] Failed to report manual command complete for %s\n", dbCommandId.c_str());
  }
}

void pollNextCommand() {
  String response;
  String url = String(BASE_URL) + "/esp32/" + MACHINE_CODE + "/next-command";

  if (!httpGet(url, response)) {
    return;
  }

  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, response);
  if (err) {
    Serial.printf("[machine] JSON parse failed: %s\n", err.c_str());
    return;
  }

  bool hasCommand = doc["has_command"] | false;
  if (!hasCommand) {
    Serial.println("[machine] No command available");
    return;
  }

  JsonObject command = doc["command"];
  String dbCommandId = command["db_command_id"] | "";
  String type = command["type"] | "";
  String machineCodeFromServer = command["machine_code"] | "";
  String sessionId = command["session_id"] | "";
  int durationMinutes = command["duration_minutes"] | 0;
  int steps = command["steps"] | 1;

  Serial.println("[machine] Command payload received");
  Serial.printf("[machine] db_command_id=%s\n", dbCommandId.c_str());
  Serial.printf("[machine] type=%s\n", type.c_str());
  Serial.printf("[machine] machine_code=%s\n", machineCodeFromServer.c_str());
  Serial.printf("[machine] session_id=%s\n", sessionId.c_str());
  Serial.printf("[machine] duration_minutes=%d\n", durationMinutes);
  Serial.printf("[machine] steps=%d\n", steps);

  if (dbCommandId.isEmpty() || type.isEmpty()) {
    Serial.println("[machine] Invalid command payload");
    return;
  }

  if (!machineCodeFromServer.isEmpty() && machineCodeFromServer != MACHINE_CODE) {
    Serial.println("[machine] Ignored command for wrong machine");
    return;
  }

  if (dbCommandId == lastExecutedCommandId || dbCommandId == activeCommandId) {
    Serial.println("[machine] Duplicate command detected, ignoring");
    return;
  }

  if (type == "START_SERVICE" && (machineRunning || finishPending)) {
    Serial.println("[machine] Busy with active wash, cannot start another session");
    return;
  }

  Serial.println("[machine] Sending ACK before execution");
  if (!ackCommand(dbCommandId, sessionId)) {
    Serial.println("[machine] ACK failed, backend will offer command again");
    return;
  }

  if (type == "START_SERVICE") {
    if (sessionId.isEmpty() || durationMinutes <= 0) {
      Serial.println("[machine] Invalid START_SERVICE payload");
      return;
    }
    startMachineRun(dbCommandId, sessionId, durationMinutes);
    return;
  }

  executeManualCommand(dbCommandId, type, steps);
}

void setup() {
  Serial.begin(115200);

  pinMode(POWER_BUTTON_PIN, OUTPUT);
  pinMode(START_PAUSE_BUTTON_PIN, OUTPUT);
  pinMode(KNOB_PIN_1, OUTPUT);
  pinMode(KNOB_PIN_2, OUTPUT);

  setButtonIdle(POWER_BUTTON_PIN);
  setButtonIdle(START_PAUSE_BUTTON_PIN);
  setKnobIdle();

  connectWiFi();
  sendHeartbeat();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] Disconnected, reconnecting");
    connectWiFi();
  }

  unsigned long nowMs = millis();

  if (nowMs - lastHeartbeatAtMs >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatAtMs = nowMs;
    sendHeartbeat();
  }

  if (nowMs - lastPollAtMs >= POLL_INTERVAL_MS) {
    lastPollAtMs = nowMs;
    pollNextCommand();
  }

  if (machineRunning && nowMs - runStartedAtMs >= runDurationMs) {
    Serial.println("[machine] Paid run duration complete");
    stopMachineRun();
  }

  maybeSendFinished();
  delay(100);
}
