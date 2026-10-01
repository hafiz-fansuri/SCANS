/*
=============================================================
  VESSEL SENSOR — ESP32  (Starlink WiFi + MQTT/TLS, cloud relay)  v7.2
  BNO08x IMU (rotation vector + linear accel) + Airmar NMEA
  → published over MQTT/TLS to a cloud broker
  → ALSO logged locally to an SD card, in TWO forms:
      1) /vessel_raw_log.csv   — parsed per-parameter telemetry
      2) /vessel_result.csv    — mirrors the laptop bridge's CSV exactly

  WHAT'S NEW IN v7.2:
  ─────────────────────────────────────────────────────────
  - FIXED: checkFullResetButton() never actually implemented the
    "hold for RESET_BUTTON_HOLD_MS" debounce the v6.8 changelog
    described — RESET_BUTTON_HOLD_MS wasn't even defined, and
    resetButtonPressedAt was declared but never read/written. The
    button fired an immediate full reboot + WiFi credential wipe
    on the very first LOW read, meaning ordinary boat vibration or
    contact bounce could silently drop the ESP32 into config-portal
    mode mid-trial. Now genuinely requires a continuous 1.5s hold
    before triggering, exactly as originally documented.
  - FIXED: RAW_LOG_HEADER labeled a column "wave_hs_m" in the
    human-readable block, but the value actually written there
    (waveHsCm) is centimetres, not metres — a mislabeled unit,
    same class of bug fixed on the Python bridge side. Column
    renamed to wave_hs_cm; no value/logic change.
  - REMOVED: fix_quality_desc text column from the SD raw log.
    It duplicated information already present in the raw
    fix_quality int a few columns earlier in the same row, and was
    the only non-numeric field in an otherwise all-numeric CSV.
    fixQualityDesc() is kept and still used for Serial debug output.

  (v7.1 and earlier changelog — SD files surviving reboot, cm wave
  height, UART buffer overrun fix, full-reset+WiFi-forget, human
  timestamps, lag fix, on-device NMEA parsing/wave estimator,
  watchdog crash-loop fix — all unchanged, see prior revisions.)
=============================================================
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>
#include <PubSubClient.h>
#include <LittleFS.h>
#include <time.h>
#include <esp_task_wdt.h>
#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include "SparkFun_BNO08x_Arduino_Library.h"

#define MOCK_AIRMAR true

// ── WiFi — handled by WiFiManager ───────────────────────────
WiFiManager wm;
#define WIFI_RESET_PIN 0
#define WIFI_AP_NAME "Jati9-Setup"
#define WIFI_AP_PASS "123456789"

// ── MQTT broker config ──────────────────────────────────────
// NOTE: this MUST be a DIFFERENT credential than the Python bridge
// uses (JATI6_MQTT_USER/JATI6_MQTT_PASS on that side). Sharing one
// credential across both ends means a leak of either file compromises
// both. Create a separate HiveMQ credential for the ESP32 before this
// repo goes anywhere public.
const char* MQTT_HOST = "d97c13f07e614f59a775fa47312851ad.s1.eu.hivemq.cloud";
const int   MQTT_PORT = 8883;
const char* MQTT_USER = "ESP";
const char* MQTT_PASS = "12082003";
const char* MQTT_CLIENT_ID = "jati6-vessel-01";
const char* TOPIC_DATA          = "jati6/raw";
const char* TOPIC_STATUS        = "jati6/status";
const char* TOPIC_RESULT        = "jati6/results";
const char* TOPIC_RESULT_HEADER = "jati6/results_header";
const char* TOPIC_COMMAND       = "jati6/command";

// ── Pins ─────────────────────────────────────────────────────
#define I2C_SDA    21
#define I2C_SCL    22
#define AIRMAR_RX  16
#define AIRMAR_TX  17

#define SD_CS    13
#define SD_SCK   12
#define SD_MOSI  14
#define SD_MISO  27

// ── the ONLY 2 SD CSVs ───────────────────────────────────────
const char* SD_LOG_PATH    = "/backup.csv";  // #1: parsed per-parameter log (WiFi-independent)
const char* SD_RESULT_PATH = "/vessel_mirror.csv";   // #2: mirrors the laptop bridge's CSV exactly

#define QUEUE_FILE       "/queue_a.txt"
#define QUEUE_FLUSHING   "/queue_b.txt"
#define QUEUE_MAX_BYTES  (2 * 1024 * 1024)

#define WDT_TIMEOUT_S 30

// ── how often IMU lines get broadcast (MQTT publish + SD append).
const unsigned long IMU_PUBLISH_MS = 100;   // 10Hz to MQTT/SD instead of 100Hz

// ── time to let the Airmar finish its own boot/handshake ──────
const unsigned long AIRMAR_BOOT_DELAY_MS = 15000;

// ── local timezone offset for the human-readable SD timestamp ──
const long GMT_OFFSET_SEC = 8 * 3600;   // UTC+8 = Malaysia
const int  DAYLIGHT_OFFSET_SEC = 0;

// ── physical full-reset button. Pull to GND to trigger.
// Wiring: one leg to GPIO25, other leg to GND. No external resistor
// needed (INPUT_PULLUP). Must be HELD for RESET_BUTTON_HOLD_MS to
// avoid accidental trips from vibration/bumps on the boat — this is
// now actually enforced (see checkFullResetButton()).
#define RESET_BUTTON_PIN      25
#define RESET_BUTTON_HOLD_MS  1500UL
unsigned long resetButtonPressedAt = 0;
bool resetButtonActive = false;

// ── full-reset flag, set from MQTT callback (FULL_RESET) ──
volatile bool fullResetRequested = false;

BNO08x         imu;
HardwareSerial AirmarSerial(2);
WiFiClientSecure tlsClient;
PubSubClient   mqtt(tlsClient);

SPIClass sdSPI(HSPI);
bool     sdReady = false;

// ── SD file #1 — raw per-parameter log (WiFi-independent) ──────
bool sdRawLogReady = false;
File sdLogFile;
unsigned long lastRawLogT = 0;
const unsigned long RAW_LOG_INTERVAL_MS = 1000;   // 1 row/second

// Row layout: a RAW block (values taken straight off the NMEA/IMU
// sentences, only string->number conversion applied) followed by a
// HUMAN-READABLE block (decimal coordinates, km/h speed, cm wave
// height, roll/pitch/yaw in degrees). fix_quality_desc text column
// removed in v7.2 — the raw fix_quality int a few columns earlier
// already carries that information; fixQualityDesc() is still used
// for Serial debug output.
const char* RAW_LOG_HEADER =
  "timestamp,"
  "lat_raw,lon_raw,gps_fix,gps_sats,hdop,altitude_m,sog_kn,"
  "wind_dir_deg,wind_kn_raw,baro_bar,air_temp_c,wave_hs_m,"
  "lat_decimal,lon_decimal,sog_kmh,"
  "roll_deg,pitch_deg,yaw_deg";

// ── SD file #2 — mirrors the laptop bridge's CSV over MQTT ──────
bool sdResultReady        = false;
bool sdResultIsNewFile    = false;
bool sdResultHeaderWritten = false;
File sdResultFile;

// ── on-device parsed telemetry (kept fresh regardless of WiFi) ──
float lastWindKn     = 0.0f;
float lastWindDirDeg = 0.0f;
float lastSogKn      = 0.0f;
int   lastGpsFix     = 0;
int   lastGpsSats    = 0;
float lastHdopVal    = 0.0f;
float lastAltitudeM  = 0.0f;
String lastLatRaw = "", lastLatHemi = "";
String lastLonRaw = "", lastLonHemi = "";
float lastBaroBar   = 0.0f;
float lastAirTempC  = 0.0f;
float lastRollDeg = 0.0f, lastPitchDeg = 0.0f, lastYawDeg = 0.0f;

// ── on-device wave estimator (port of Python WaveEstimator) ────
const float WAVE_HP_FC     = 0.06f;
const float WAVE_HP_FC2    = 0.05f;
const float WAVE_VARIANCE_TAU_RISE_S = 10.0f;
const float WAVE_VARIANCE_TAU_FALL_S = 2.0f;
const float WAVE_HS_ZERO_FLOOR_M     = 0.02f;
const int   WAVE_WARMUP    = 100;
const float WAVE_ROAD_MAX  = 3.0f;
//float waveAlphaW = 1.0f - 1.0f / WAVE_WIN_SAMP;
float waveHp1In = 0, waveHp1Out = 0, waveVel = 0;
float waveHp2In = 0, waveHp2Out = 0, waveDisp = 0;
float waveWm = 0, waveWs = 0;
int   waveN = 0;
float lastWaveHsM = 0.0f;
unsigned long waveLastT = 0;
bool  waveHaveLastT = false;

// ── soft reset flag, set from MQTT callback, handled in loop() ──
volatile bool softResetRequested = true;

float baselineAz = 0.0f;
unsigned long lastImuSend = 0;
unsigned long lastImuPublish = 0;

float qw = 1, qx = 0, qy = 0, qz = 0;
bool  haveQuat = false;

bool queueFileOpenForWrite = false;
File queueWriteFile;

// ── quaternion helpers ────────────────────────────────────────
void quatToEuler(float w, float x, float y, float z,
                  float &rollDeg, float &pitchDeg, float &yawDeg) {
  float sinr_cosp = 2.0f * (w * x + y * z);
  float cosr_cosp = 1.0f - 2.0f * (x * x + y * y);
  rollDeg = atan2(sinr_cosp, cosr_cosp) * 180.0f / PI;

  float sinp = 2.0f * (w * y - z * x);
  sinp = constrain(sinp, -1.0f, 1.0f);
  pitchDeg = asin(sinp) * 180.0f / PI;

  float siny_cosp = 2.0f * (w * z + x * y);
  float cosy_cosp = 1.0f - 2.0f * (y * y + z * z);
  yawDeg = atan2(siny_cosp, cosy_cosp) * 180.0f / PI;
}

void rotateVecByQuat(float w, float x, float y, float z,
                      float vx, float vy, float vz,
                      float &ox, float &oy, float &oz) {
  float tx = 2.0f * (y * vz - z * vy);
  float ty = 2.0f * (z * vx - x * vz);
  float tz = 2.0f * (x * vy - y * vx);
  ox = vx + w * tx + (y * tz - z * ty);
  oy = vy + w * ty + (z * tx - x * tz);
  oz = vz + w * tz + (x * ty - y * tx);
}

// ══════════════════════════════════════════════════════════════
//  TIME
// ══════════════════════════════════════════════════════════════
bool timeSynced = false;

void syncTimeNTP() {
  configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, "pool.ntp.org", "time.google.com");
  Serial.println("INFO:NTP_SYNCING");
  time_t now = time(nullptr);
  unsigned long t0 = millis();
  while (now < 1700000000 && millis() - t0 < 10000) {
    esp_task_wdt_reset();
    delay(200);
    now = time(nullptr);
  }
  timeSynced = (now > 1700000000);
  Serial.print("INFO:NTP_"); Serial.println(timeSynced ? "OK" : "TIMEOUT");
}

unsigned long long nowEpochMs() {
  if (timeSynced) {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return (unsigned long long)tv.tv_sec * 1000ULL + tv.tv_usec / 1000ULL;
  }
  return (unsigned long long)millis();
}

String nowTimestampString() {
  if (timeSynced) {
    time_t now = time(nullptr);
    struct tm timeinfo;
    localtime_r(&now, &timeinfo);
    char buf[24];
    snprintf(buf, sizeof(buf), "%04d/%02d/%02d %02d:%02d:%02d",
             timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
             timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    return String(buf);
  }
  return "UPTIME_" + String(millis());
}

// ══════════════════════════════════════════════════════════════
//  WAVE ESTIMATOR — port of Python bridge's WaveEstimator class
// ══════════════════════════════════════════════════════════════
float waveEstimatorUpdate(float az, unsigned long tMs) {
  if (!waveHaveLastT) {
    waveLastT = tMs;
    waveHaveLastT = true;
    return lastWaveHsM;
  }
  float dt = (tMs - waveLastT) / 1000.0f;
  waveLastT = tMs;
  if (dt <= 0.0f || dt > 0.5f) return lastWaveHsM;

  float a1 = (1.0f / (2.0f * PI * WAVE_HP_FC)) / (1.0f / (2.0f * PI * WAVE_HP_FC) + dt);
  float a2 = (1.0f / (2.0f * PI * WAVE_HP_FC2)) / (1.0f / (2.0f * PI * WAVE_HP_FC2) + dt);

  float azHp = a1 * (waveHp1Out + az - waveHp1In);
  waveHp1In = az; waveHp1Out = azHp;
  waveVel += azHp * dt;
  float velHp = a2 * (waveHp2Out + waveVel - waveHp2In);
  waveHp2In = waveVel; waveHp2Out = velHp;
  waveDisp += velHp * dt;

  waveN++;
  float delta = waveDisp - waveWm;
  float instVar = delta * delta;
  float tau = (instVar > waveWs) ? WAVE_VARIANCE_TAU_RISE_S : WAVE_VARIANCE_TAU_FALL_S;
  float alphaW = exp(-dt / tau);
  waveWm = alphaW * waveWm + (1 - alphaW) * waveDisp;
  float delta2 = waveDisp - waveWm;
  waveWs = alphaW * waveWs + (1 - alphaW) * delta * delta2;

  if (waveN > WAVE_WARMUP) {
    float hs = 4.0f * sqrt(max(waveWs, 0.0f));
    hs = constrain(hs, 0.0f, WAVE_ROAD_MAX);
    lastWaveHsM = (hs > WAVE_HS_ZERO_FLOOR_M) ? hs : 0.0f;
  }
  return lastWaveHsM;
}

void resetWaveEstimator() {
  waveHp1In = waveHp1Out = waveVel = 0;
  waveHp2In = waveHp2Out = waveDisp = 0;
  waveWm = waveWs = 0;
  waveN = 0;
  lastWaveHsM = 0.0f;
  waveHaveLastT = false;
}

// ══════════════════════════════════════════════════════════════
//  NMEA FIELD PARSER + WIND/SOG/GPS/WEATHER PARSING (on-device)
// ══════════════════════════════════════════════════════════════
String nmeaField(const String &sentence, int n) {
  int start = 0, commaCount = 0;
  for (int i = 0; i < (int)sentence.length(); i++) {
    if (sentence[i] == ',') {
      if (commaCount == n) {
        String val = sentence.substring(start, i);
        int star = val.indexOf('*');
        if (star >= 0) val = val.substring(0, star);
        val.trim();
        return val;
      }
      commaCount++;
      start = i + 1;
    }
  }
  if (commaCount == n) {
    String val = sentence.substring(start);
    int star = val.indexOf('*');
    if (star >= 0) val = val.substring(0, star);
    val.trim();
    return val;
  }
  return "";
}

bool parseWimwv(const String &sentence, float &knots, float &dirDeg) {
  if (nmeaField(sentence, 5) != "A") return false;
  String dirStr = nmeaField(sentence, 1);
  if (dirStr.length()) dirDeg = dirStr.toFloat();
  String spdStr = nmeaField(sentence, 3);
  if (spdStr.length() == 0) return false;
  float speed = spdStr.toFloat();
  String unit = nmeaField(sentence, 4);
  if (unit == "K" || unit == "k") speed *= 0.539957f;
  else if (unit == "M" || unit == "m") speed *= 1.94384f;
  knots = max(0.0f, speed);
  return true;
}

void parseGpgga(const String &sentence, int &fix, int &sats,
                 String &latRaw, String &latHemi,
                 String &lonRaw, String &lonHemi,
                 float &hdop, float &altitudeM) {
  latRaw  = nmeaField(sentence, 2);
  latHemi = nmeaField(sentence, 3);
  lonRaw  = nmeaField(sentence, 4);
  lonHemi = nmeaField(sentence, 5);
  String fixStr  = nmeaField(sentence, 6);
  String satsStr = nmeaField(sentence, 7);
  String hdopStr = nmeaField(sentence, 8);
  String altStr  = nmeaField(sentence, 9);
  fix       = fixStr.length()  ? fixStr.toInt()   : 0;
  sats      = satsStr.length() ? satsStr.toInt()  : 0;
  hdop      = hdopStr.length() ? hdopStr.toFloat() : 0.0f;
  altitudeM = altStr.length()  ? altStr.toFloat()  : 0.0f;
}

bool parseSog(const String &sentence, float &sogKn) {
  String prefix = sentence.substring(0, 6);
  prefix.toUpperCase();
  if (prefix == "$GPRMC" || prefix == "$GNRMC") {
    if (nmeaField(sentence, 2) != "A") return false;
    String s = nmeaField(sentence, 7);
    if (s.length() == 0) return false;
    float v = s.toFloat();
    if (v >= 0.0f && v < 50.0f) { sogKn = v; return true; }
    return false;
  }
  if (prefix == "$GPVTG" || prefix == "$GNVTG" || prefix == "$IIVTG") {
    String s = nmeaField(sentence, 5);
    if (s.length() == 0) return false;
    float v = s.toFloat();
    if (v >= 0.0f && v < 50.0f) { sogKn = v; return true; }
    return false;
  }
  return false;
}

bool parseWimda(const String &sentence, float &baroBar, float &airTempC) {
  String barStr  = nmeaField(sentence, 3);
  String tempStr = nmeaField(sentence, 5);
  if (barStr.length() == 0 && tempStr.length() == 0) return false;
  if (barStr.length())  baroBar  = barStr.toFloat();
  if (tempStr.length()) airTempC = tempStr.toFloat();
  return true;
}

void parseAirmarLine(const String &line) {
  if (line.startsWith("$WIMWV")) {
    float w, d = lastWindDirDeg;
    if (parseWimwv(line, w, d)) { lastWindKn = w; lastWindDirDeg = d; }
  } else if (line.startsWith("$GPGGA") || line.startsWith("$GNGGA")) {
    int fix, sats;
    String latRaw, latHemi, lonRaw, lonHemi;
    float hdop, alt;
    parseGpgga(line, fix, sats, latRaw, latHemi, lonRaw, lonHemi, hdop, alt);
    lastGpsFix = fix; lastGpsSats = sats;
    if (latRaw.length()) { lastLatRaw = latRaw; lastLatHemi = latHemi; }
    if (lonRaw.length()) { lastLonRaw = lonRaw; lastLonHemi = lonHemi; }
    lastHdopVal = hdop; lastAltitudeM = alt;
  } else if (line.startsWith("$GPRMC") || line.startsWith("$GNRMC") ||
             line.startsWith("$GPVTG") || line.startsWith("$GNVTG") || line.startsWith("$IIVTG")) {
    float s;
    if (parseSog(line, s)) lastSogKn = s;
  } else if (line.startsWith("$WIMDA") || line.startsWith("$IIMDA") || line.startsWith("$YXMDA")) {
    float bar = lastBaroBar, temp = lastAirTempC;
    if (parseWimda(line, bar, temp)) { lastBaroBar = bar; lastAirTempC = temp; }
  }
}

float nmeaCoordToDecimal(const String &raw, const String &hemi, bool isLon) {
  int degLen = isLon ? 3 : 2;
  if (raw.length() <= (unsigned)degLen) return 0.0f;
  float degrees = raw.substring(0, degLen).toFloat();
  float minutes = raw.substring(degLen).toFloat();
  float dec = degrees + minutes / 60.0f;
  if (hemi == "S" || hemi == "W") dec = -dec;
  return dec;
}

const char* fixQualityDesc(int fixQuality) {
  switch (fixQuality) {
    case 0:  return "no_fix";
    case 1:  return "gps_fix";
    case 2:  return "dgps_fix";
    case 4:  return "rtk_fixed";
    case 5:  return "rtk_float";
    case 6:  return "estimated";
    default: return "unknown";
  }
}

// ══════════════════════════════════════════════════════════════
//  SD CARD LOGGING — file #1: raw per-parameter log
// ══════════════════════════════════════════════════════════════
void initSD() {
  sdSPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);

  if (!SD.begin(SD_CS, sdSPI)) {
    Serial.println("WARN:SD_MOUNT_FAILED — continuing without local SD logging");
    sdReady = false;
    return;
  }
  if (SD.cardType() == CARD_NONE) {
    Serial.println("WARN:SD_NO_CARD_DETECTED");
    sdReady = false;
    return;
  }

  bool logIsNewFile = !SD.exists(SD_LOG_PATH);

  sdLogFile = SD.open(SD_LOG_PATH, FILE_APPEND);
  if (!sdLogFile) {
    Serial.println("WARN:SD_FILE_OPEN_FAILED");
    sdReady = false;
    return;
  }
  if (logIsNewFile) {
    sdLogFile.println(RAW_LOG_HEADER);
  }
  sdRawLogReady = true;
  sdReady = true;
  Serial.print("INFO:SD_OK logging raw per-parameter telemetry to "); Serial.println(SD_LOG_PATH);
  Serial.println(logIsNewFile ? "INFO:SD_RAW_LOG_NEW_FILE" : "INFO:SD_RAW_LOG_APPENDING_EXISTING");
}

// One row per second — RAW block first, then HUMAN-READABLE block.
void logRawParamsRow() {
  if (!sdRawLogReady) return;
  unsigned long now = millis();
  if (now - lastRawLogT < RAW_LOG_INTERVAL_MS) return;
  lastRawLogT = now;

  float latDecimal = nmeaCoordToDecimal(lastLatRaw, lastLatHemi, false);
  float lonDecimal = nmeaCoordToDecimal(lastLonRaw, lastLonHemi, true);
  float sogKmh      = lastSogKn * 1.852f;

  // ── RAW block ────────────────────────────────────────────────
  sdLogFile.print(nowTimestampString());        sdLogFile.print(",");
  sdLogFile.print(lastLatRaw);                   sdLogFile.print(",");
  sdLogFile.print(lastLonRaw);                   sdLogFile.print(",");
  sdLogFile.print(lastGpsFix);                   sdLogFile.print(",");
  sdLogFile.print(lastGpsSats);                  sdLogFile.print(",");
  sdLogFile.print(lastHdopVal, 2);               sdLogFile.print(",");
  sdLogFile.print(lastAltitudeM, 2);             sdLogFile.print(",");
  sdLogFile.print(lastSogKn, 3);                 sdLogFile.print(",");
  sdLogFile.print(lastWindDirDeg, 1);            sdLogFile.print(",");
  sdLogFile.print(lastWindKn, 2);                sdLogFile.print(",");
  sdLogFile.print(lastBaroBar, 4);               sdLogFile.print(",");
  sdLogFile.print(lastAirTempC, 1);              sdLogFile.print(",");
  sdLogFile.print(lastWaveHsM, 4);               sdLogFile.print(",");   // metres, only representation now

  // ── HUMAN-READABLE block ─────────────────────────────────────
  sdLogFile.print(latDecimal, 6);                sdLogFile.print(",");
  sdLogFile.print(lonDecimal, 6);                sdLogFile.print(",");
  sdLogFile.print(sogKmh, 3);                    sdLogFile.print(",");
  sdLogFile.print(lastRollDeg, 2);                sdLogFile.print(",");
  sdLogFile.print(lastPitchDeg, 2);               sdLogFile.print(",");
  sdLogFile.println(lastYawDeg, 2);

  sdLogFile.flush();
}

// ══════════════════════════════════════════════════════════════
//  SD CARD LOGGING — file #2: result mirror
// ══════════════════════════════════════════════════════════════
void initSDResultFile() {
  if (!sdReady) return;

  bool resultFileExisted = SD.exists(SD_RESULT_PATH);
  sdResultFile = SD.open(SD_RESULT_PATH, FILE_APPEND);
  if (!sdResultFile) {
    Serial.println("WARN:SD_RESULT_FILE_OPEN_FAILED");
    sdResultReady = false;
    return;
  }

  sdResultIsNewFile = (!resultFileExisted) || (sdResultFile.size() == 0);
  sdResultHeaderWritten = !sdResultIsNewFile;

  sdResultReady = true;
  Serial.print("INFO:SD_RESULT_OK — mirroring laptop CSV to ");
  Serial.print(SD_RESULT_PATH);
  Serial.println(sdResultIsNewFile
                   ? " (new/empty file — waiting for header from MQTT)"
                   : " (existing file with data — appending)");
}

// ══════════════════════════════════════════════════════════════
//  FULL RESET — button (physical, GPIO25) + MQTT command
// ══════════════════════════════════════════════════════════════
void wipeWifiAndRestart(const char* reason) {
  Serial.print("INFO:WIFI_CREDS_CLEARED reason=");
  Serial.println(reason);
  wm.resetSettings();
  if (sdReady) sdLogFile.flush();
  delay(200);
  ESP.restart();
}

// v7.2 FIX: this used to fire on the very first LOW read, with
// RESET_BUTTON_HOLD_MS documented in the changelog but never
// actually defined or checked anywhere. On a boat, that meant
// ordinary vibration or a bounced contact could trigger a full
// reboot + WiFi credential wipe mid-trial. Now genuinely requires
// a continuous hold of RESET_BUTTON_HOLD_MS before it fires.
void checkFullResetButton() {
  bool pressed = (digitalRead(RESET_BUTTON_PIN) == LOW);

  if (pressed && !resetButtonActive) {
    resetButtonActive = true;
    resetButtonPressedAt = millis();
  } else if (pressed && resetButtonActive) {
    if (millis() - resetButtonPressedAt >= RESET_BUTTON_HOLD_MS) {
      wipeWifiAndRestart("physical_button_held");
    }
  } else if (!pressed) {
    resetButtonActive = false;
  }
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String msg;
  msg.reserve(length + 1);
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

  if (strcmp(topic, TOPIC_RESULT_HEADER) == 0) {
    if (sdResultReady && !sdResultHeaderWritten) {
      sdResultFile.println(msg);
      sdResultFile.flush();
      sdResultHeaderWritten = true;
      Serial.println("INFO:SD_RESULT_HEADER_WRITTEN (from MQTT, matches laptop CSV exactly)");
    }
    return;
  }

  if (strcmp(topic, TOPIC_RESULT) == 0) {
    if (!sdResultReady) return;
    sdResultFile.println(msg);
    sdResultFile.flush();
    return;
  }

  if (strcmp(topic, TOPIC_COMMAND) == 0) {
    if (msg == "SOFT_RESET") {
      Serial.println("INFO:SOFT_RESET_QUEUED");
      softResetRequested = true;
    } else if (msg == "FULL_RESET") {
      Serial.println("INFO:FULL_RESET_QUEUED (via MQTT)");
      fullResetRequested = true;
    }
    return;
  }
}

// ══════════════════════════════════════════════════════════════
//  STORE-AND-FORWARD (LittleFS)
// ══════════════════════════════════════════════════════════════
void queueOpenForWrite() {
  if (queueFileOpenForWrite) return;
  queueWriteFile = LittleFS.open(QUEUE_FILE, FILE_APPEND);
  queueFileOpenForWrite = (bool)queueWriteFile;
}

void queueLine(const String &line) {
  queueOpenForWrite();
  if (!queueWriteFile) return;

  if (queueWriteFile.size() > QUEUE_MAX_BYTES) {
    queueWriteFile.close();
    LittleFS.remove(QUEUE_FILE);
    Serial.println("WARN:QUEUE_OVERFLOW_DROPPED");
    queueWriteFile = LittleFS.open(QUEUE_FILE, FILE_APPEND);
  }
  queueWriteFile.println(line);
  queueWriteFile.flush();
}

void queueFlushSome(int maxLines = 20) {
  if (!LittleFS.exists(QUEUE_FILE)) return;

  if (queueFileOpenForWrite) {
    queueWriteFile.close();
    queueFileOpenForWrite = false;
  }
  if (!LittleFS.exists(QUEUE_FLUSHING)) {
    if (!LittleFS.rename(QUEUE_FILE, QUEUE_FLUSHING)) return;
  }

  File f = LittleFS.open(QUEUE_FLUSHING, FILE_READ);
  if (!f) return;

  String remaining = "";
  int sent = 0;
  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;
    if (sent < maxLines && mqtt.connected()) {
      if (mqtt.publish(TOPIC_DATA, line.c_str())) {
        sent++;
        continue;
      }
    }
    remaining += line + "\n";
  }
  f.close();

  if (remaining.length() == 0) {
    LittleFS.remove(QUEUE_FLUSHING);
    if (sent > 0) { Serial.print("INFO:QUEUE_DRAINED sent="); Serial.println(sent); }
  } else {
    File rest = LittleFS.open(QUEUE_FLUSHING, FILE_WRITE);
    rest.print(remaining);
    rest.close();
    if (sent > 0) { Serial.print("INFO:QUEUE_PARTIAL sent="); Serial.println(sent); }
  }
}

// ══════════════════════════════════════════════════════════════
//  PUBLISH
// ══════════════════════════════════════════════════════════════
void broadcastLine(const String &rawLine) {
  unsigned long long ts = nowEpochMs();
  String envelope = "T" + String((unsigned long long)ts) + "|" + rawLine;

  Serial.println(envelope);

  parseAirmarLine(rawLine);

  if (mqtt.connected()) {
    if (!mqtt.publish(TOPIC_DATA, envelope.c_str())) {
      queueLine(envelope);
    }
  } else {
    queueLine(envelope);
  }
}

// ── non-blocking WiFi reconnect ────────────────────────────────
unsigned long lastWifiAttempt = 0;
const unsigned long WIFI_RETRY_MS = 5000;

void maintainWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  unsigned long now = millis();
  if (now - lastWifiAttempt < WIFI_RETRY_MS) return;
  lastWifiAttempt = now;
  Serial.println("INFO:WIFI_RECONNECTING");
  WiFi.disconnect();
  WiFi.begin();
}

// ── non-blocking MQTT reconnect ────────────────────────────────
unsigned long lastMqttAttempt = 0;
const unsigned long MQTT_RETRY_MS = 5000;

void maintainMqtt() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqtt.connected()) { mqtt.loop(); return; }

  unsigned long now = millis();
  if (now - lastMqttAttempt < MQTT_RETRY_MS) return;
  lastMqttAttempt = now;

  Serial.println("INFO:MQTT_CONNECTING");
  bool ok = mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS,
                         TOPIC_STATUS, 1, true, "offline");
  if (ok) {
    mqtt.publish(TOPIC_STATUS, "online", true);
    mqtt.subscribe(TOPIC_RESULT);
    mqtt.subscribe(TOPIC_RESULT_HEADER);
    mqtt.subscribe(TOPIC_COMMAND);
    Serial.println("INFO:MQTT_OK (subscribed to results + results_header + command)");
  } else {
    Serial.print("WARN:MQTT_FAILED rc="); Serial.println(mqtt.state());
  }
}

// ── NMEA checksum / commands ───────────────────────────────────
String nmeaChecksum(String s) {
  byte cs = 0;
  for (int i = 0; i < (int)s.length(); i++) cs ^= (byte)s[i];
  char buf[3];
  sprintf(buf, "%02X", cs);
  return String(buf);
}

void sendAirmarCmd(String body) {
  String cmd = "$" + body + "*" + nmeaChecksum(body);
  AirmarSerial.println(cmd);
  Serial.print("INFO:CMD="); Serial.println(cmd);
  delay(400);
}

String airmarLineBuf = "";

void pollAirmarPassthrough() {
  while (AirmarSerial.available()) {
    char c = (char)AirmarSerial.read();
    if (c == '\n') {
      airmarLineBuf.trim();
      if (airmarLineBuf.length() > 0) broadcastLine(airmarLineBuf);
      airmarLineBuf = "";
    } else if (c != '\r') {
      airmarLineBuf += c;
      if (airmarLineBuf.length() > 120) airmarLineBuf = "";
    }
  }
}

// ══════════════════════════════════════════════════════════════
//  MOCK AIRMAR
// ══════════════════════════════════════════════════════════════
unsigned long lastMockNmea = 0;
float mockWindPhase = 0.0f;
float mockSogPhase  = 0.0f;

void sendNmeaLine(const String &body) {
  broadcastLine("$" + body + "*" + nmeaChecksum(body));
}

void sendMockAirmar() {
  unsigned long now = millis();
  if (now - lastMockNmea < 1000) return;
  lastMockNmea = now;

  mockWindPhase += 0.15f;
  mockSogPhase  += 0.08f;

  float windKn = 6.0f + 3.0f * sin(mockWindPhase) + (random(-20, 20) / 100.0f);
  if (windKn < 0) windKn = 0;
  float sogKn = 19.0f + 2.5f * sin(mockSogPhase) + (random(-15, 15) / 100.0f);
  if (sogKn < 0) sogKn = 0;

  char buf[100];
  snprintf(buf, sizeof(buf), "WIMWV,045.0,R,%.1f,N,A", windKn);
  sendNmeaLine(String(buf));
  sendNmeaLine(String("GPGGA,120000.00,,,,,1,08,1.0,0.0,M,0.0,M,,"));
  snprintf(buf, sizeof(buf), "GPRMC,120000.00,A,,,,,%.2f,000.0,010726,,,A", sogKn);
  sendNmeaLine(String(buf));
}

// ══════════════════════════════════════════════════════════════
//  WIFI — WiFiManager captive portal
// ══════════════════════════════════════════════════════════════
void connectWiFi() {
  pinMode(WIFI_RESET_PIN, INPUT_PULLUP);
  if (digitalRead(WIFI_RESET_PIN) == LOW) {
    Serial.println("INFO:WIFI_RESET_PIN_HELD — clearing saved creds this boot");
    wm.resetSettings();
  }

  wm.setConfigPortalTimeout(180);
  wm.setConfigPortalBlocking(false);

  Serial.println("INFO:WIFI_CONNECTING (WiFiManager)");
  bool startedOk = wm.autoConnect(WIFI_AP_NAME, WIFI_AP_PASS);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    wm.process();
    esp_task_wdt_reset();
    delay(50);

    if (millis() - t0 > 185000UL) {
      Serial.println("ERR:WIFI_PORTAL_TIMEOUT — restarting to retry");
      delay(1000);
      ESP.restart();
    }
  }

  Serial.print("INFO:WIFI_OK ip="); Serial.println(WiFi.localIP());
}

void setup() {
  Serial.begin(115200);
  delay(500);

  esp_task_wdt_config_t wdt_cfg = {
    .timeout_ms = WDT_TIMEOUT_S * 1000,
    .idle_core_mask = 0,
    .trigger_panic = true
  };
  esp_err_t wdtErr = esp_task_wdt_init(&wdt_cfg);
  if (wdtErr == ESP_ERR_INVALID_STATE) {
    wdtErr = esp_task_wdt_reconfigure(&wdt_cfg);
    Serial.print("INFO:WDT_RECONFIGURED err="); Serial.println(wdtErr);
  } else if (wdtErr != ESP_OK) {
    Serial.print("WARN:WDT_INIT_ERR="); Serial.println(wdtErr);
  }

  esp_err_t addErr = esp_task_wdt_add(NULL);
  if (addErr != ESP_OK && addErr != ESP_ERR_INVALID_STATE) {
    Serial.print("WARN:WDT_ADD_ERR="); Serial.println(addErr);
  }

  // full-reset button, GPIO25 to GND
  pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);

  if (!LittleFS.begin(true)) {
    Serial.println("ERR:LITTLEFS_MOUNT_FAILED");
  }

  initSD();
  initSDResultFile();

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setTimeOut(1000);
  if (!imu.begin()) {
    Serial.println("ERR:IMU_FAIL — rebooting in 5s");
    delay(5000);
    ESP.restart();
  }
  imu.enableLinearAccelerometer(10);
  imu.enableRotationVector(10);
  Serial.println("INFO:BNO085_REPORTS_ENABLED");

  AirmarSerial.setRxBufferSize(2048);
  AirmarSerial.begin(4800, SERIAL_8N1, AIRMAR_RX, AIRMAR_TX);
  delay(AIRMAR_BOOT_DELAY_MS);

  if (MOCK_AIRMAR) {
    Serial.println("INFO:MOCK_AIRMAR_ENABLED — simulating wind/SOG/GPS, real BNO085 IMU still live");
  } else {
    Serial.println("INFO:ENABLING_SOG");
    sendAirmarCmd("PAMTC,EN,RMC,1");
    sendAirmarCmd("PAMTC,EN,VTG,1");
    Serial.println("INFO:SOG_ENABLED");
  }

  connectWiFi();
  if (WiFi.status() == WL_CONNECTED) syncTimeNTP();

  tlsClient.setInsecure();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  mqtt.setCallback(mqttCallback);
  maintainMqtt();

  Serial.println("INFO:IMU_CAL");
  double sum = 0.0; int got = 0;
  unsigned long t0 = millis();
  while (got < 300 && millis() - t0 < 8000) {
    esp_task_wdt_reset();
    if (imu.getSensorEvent()) {
      uint8_t id = imu.getSensorEventID();
      if (id == SENSOR_REPORTID_ROTATION_VECTOR) {
        qw = imu.getQuatReal(); qx = imu.getQuatI();
        qy = imu.getQuatJ();    qz = imu.getQuatK();
        haveQuat = true;
      } else if (id == SENSOR_REPORTID_LINEAR_ACCELERATION && haveQuat) {
        float wx, wy, wz;
        rotateVecByQuat(qw, qx, qy, qz,
                         imu.getLinAccelX(), imu.getLinAccelY(), imu.getLinAccelZ(),
                         wx, wy, wz);
        sum += wz; got++;
      }
    }
    delay(5);
  }
  baselineAz = (got > 0) ? (float)(sum / got) : 0.0f;
  Serial.print("INFO:IMU_BASELINE_AZ="); Serial.println(baselineAz, 4);
  Serial.println("INFO:READY");
}

unsigned long lastQueueFlushCheck = 0;

void loop() {
  esp_task_wdt_reset();

  // check physical full-reset button (works with no WiFi/MQTT)
  checkFullResetButton();

  // handle a queued full-reset request (from MQTT command)
  if (fullResetRequested) {
    fullResetRequested = false;
    wipeWifiAndRestart("mqtt_command");
  }

  // ── handle a queued soft-reset request (sensors only) ────────
  if (softResetRequested) {
    softResetRequested = false;
    Serial.println("INFO:SOFT_RESET_RUNNING — recalibrating IMU + wave estimator, WiFi/MQTT untouched");
    haveQuat = false;
    qw = 1; qx = 0; qy = 0; qz = 0;
    resetWaveEstimator();

    double sum = 0.0; int got = 0;
    unsigned long t0 = millis();
    while (got < 300 && millis() - t0 < 8000) {
      esp_task_wdt_reset();
      maintainMqtt();
      if (imu.getSensorEvent()) {
        uint8_t id = imu.getSensorEventID();
        if (id == SENSOR_REPORTID_ROTATION_VECTOR) {
          qw = imu.getQuatReal(); qx = imu.getQuatI();
          qy = imu.getQuatJ();    qz = imu.getQuatK();
          haveQuat = true;
        } else if (id == SENSOR_REPORTID_LINEAR_ACCELERATION && haveQuat) {
          float wx, wy, wz;
          rotateVecByQuat(qw, qx, qy, qz,
                           imu.getLinAccelX(), imu.getLinAccelY(), imu.getLinAccelZ(),
                           wx, wy, wz);
          sum += wz; got++;
        }
      }
      delay(5);
    }
    baselineAz = (got > 0) ? (float)(sum / got) : baselineAz;
    Serial.print("INFO:SOFT_RESET_DONE new_baseline_az="); Serial.println(baselineAz, 4);
  }

  if (MOCK_AIRMAR) {
    sendMockAirmar();
  } else {
    pollAirmarPassthrough();
  }

  maintainWifi();
  maintainMqtt();

  if (millis() - lastQueueFlushCheck > 2000) {
    lastQueueFlushCheck = millis();
    if (mqtt.connected()) queueFlushSome();
  }

  if (imu.getSensorEvent()) {
    uint8_t id = imu.getSensorEventID();

    if (id == SENSOR_REPORTID_ROTATION_VECTOR) {
      qw = imu.getQuatReal(); qx = imu.getQuatI();
      qy = imu.getQuatJ();    qz = imu.getQuatK();
      haveQuat = true;
    }
    else if (id == SENSOR_REPORTID_LINEAR_ACCELERATION && haveQuat) {
      unsigned long now = millis();
      if (now - lastImuSend >= 10) {
        lastImuSend = now;

        float wx, wy, wz;
        rotateVecByQuat(qw, qx, qy, qz,
                         imu.getLinAccelX(), imu.getLinAccelY(), imu.getLinAccelZ(),
                         wx, wy, wz);
        float azWorld = wz - baselineAz;

        float rollDeg, pitchDeg, yawDeg;
        quatToEuler(qw, qx, qy, qz, rollDeg, pitchDeg, yawDeg);
        lastRollDeg = rollDeg; lastPitchDeg = pitchDeg; lastYawDeg = yawDeg;
        waveEstimatorUpdate(azWorld, now);

        if (now - lastImuPublish >= IMU_PUBLISH_MS) {
          lastImuPublish = now;
          String imuLine = "IMU:" + String(azWorld, 4) + "," + String(rollDeg, 2) + "," +
                            String(pitchDeg, 2) + "," + String(yawDeg, 2);
          broadcastLine(imuLine);
        }
      }
    }
  }

  logRawParamsRow();
}