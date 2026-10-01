# SCANS

**S**pearheading **C**onditions **A**ware **N**avigation & **S**peed-forecasting system — a real-time vessel speed prediction and telemetry dashboard that fuses wind, wave, and ship-motion data to forecast vessel speed under environmental influences.

[![GitHub repo](https://img.shields.io/badge/GitHub-hafiz--fansuri%2FSCANS-181717?logo=github)](https://github.com/hafiz-fansuri/SCANS)


> Capstone / Final-Year Project by Muhammad Haznan bin Haznan (fansuri).
> Field-tested aboard MV *Jati 6* during sea trials.

---

## Background

Maritime vessels lose efficiency when operating in adverse environmental conditions (headwinds, head seas). This project builds a hybrid physics-ML pipeline that:

1. **Collects** onboard NMEA telemetry and IMU motion data from an ESP32-based sensor node.
2. **Estimates** true wind, significant wave height, and vessel attitude in real time.
3. **Forecasts** vessel speed using a hybrid model: a Physics-Informed Neural Network (PINN) residual corrects a closed-form physics model, and an XGBoost regressor maps the combined features to the final speed prediction.
4. **Streams** live telemetry to a browser-based dashboard over WebSocket.

The system communicates over MQTT/TLS to a cloud broker (HiveMQ), so it works over Starlink/marina WiFi without a local network. An ngrok tunnel can optionally expose the dashboard publicly.

---

## Objectives

| # | Objective | Status |
|---|-----------|--------|
| 1 | Capture NMEA sentences (wind, GPS, SOG) + BNO08x IMU linear acceleration | Done |
| 2 | Derive true wind and significant wave height (Hs) on-device and on the bridge | Done |
| 3 | Train a hybrid ML model (physics + PINN + XGBoost) to forecast vessel speed | Done |
| 4 | Stream live telemetry (SOG, wind, wave, forecast) to a web dashboard | Done |
| 5 | Log sensor data locally to SD card and to rolling CSV for post-analysis | Done |
| 6 | Provide a soft-reset / full-reset mechanism for field robustness | Done |

---

## Hardware

| Component | Purpose |
|-----------|---------|
| **ESP32** development board | Sensor node: reads IMU + NMEA, runs WiFi, MQTT, SD logging |
| **BNO085 (BNO08x)** 9-axis IMU | Provides rotation vector (quaternion) + linear acceleration for roll/pitch/yaw and wave estimation |
| **Airmar weather station** (or compatible NMEA-0183) | Wind speed/dir (`$WIMWV`), GPS fix (`$GPGGA`), SOG (`$GPRMC`/`$GPVTG`), barometric pressure + air temp (`$WIMDA`) |
| **MicroSD card** | Local backup log (`/backup.csv`) + mirror of bridge CSV (`/vessel_mirror.csv`) |
| **Physical reset button** | GPIO25, held 1.5s to wipe WiFi credentials and reboot |
| **WiFi** | Connects to vessel's Starlink/marina network (captive-portal setup via WiFiManager) |

### ESP32 Pinout

| Peripheral | ESP32 Pin |
|------------|-----------|
| BNO08x I2C SDA | GPIO21 |
| BNO08x I2C SCL | GPIO22 |
| Airmar UART RX | GPIO16 |
| Airmar UART TX | GPIO17 |
| SD card CS | GPIO13 |
| SD card SCK | GPIO12 |
| SD card MOSI | GPIO14 |
| SD card MISO | GPIO27 |
| WiFi reset button | GPIO0 |
| Full-reset button | GPIO25 |

---

## Software Stack

| Layer | Technology |
|-------|-----------|
| **Firmware** | ESP32 Arduino C++ (`SCANS.ino`) |
| **Inference bridge** | Python 3 (`main.py`) — paho-mqtt, numpy, torch, xgboost, joblib, matplotlib, requests |
| **Dashboard server** | Node.js + Express + ws (`server.js`) |
| **Frontend** | HTML/CSS/JS, Chart.js, Three.js, Leaflet (live at `vessel-dashboard/public/index.html`) |
| **Post-cruise analysis** | Python 3 + pandas (`analysis.py`) |
| **Model training** | Jupyter notebook + Python (XGBoost + PyTorch, scripts in `Training/`) |
| **Model export** | onnxmltools — converts XGBoost to ONNX for portability (`Training/checkpoints_fyp(vessel_forecast)/co.py`) |
| **Tunnel** | ngrok (optional, for public dashboard access) |

### Arduino Library Dependencies (ESP32)

Install via Arduino Library Manager:

- WiFi
- WiFiManager (tzapu / wifi-manager)
- PubSubClient (Nick O'Leary)
- LittleFS_esp32
- SparkFun BNO08x Arduino Library (SparkFun)

---

## Project Structure

```
SCANS/
├─ ESP CODE/
│   └─ SCANS.ino                          # ESP32 firmware (v7.2)
│
├─ vessel-dashboard/
│   ├─ server.js                          # Node.js Express + WebSocket server
│   ├─ package.json                       # Node deps (express, ws)
│   ├─ start.bat                          # Windows launcher (server + ngrok + bridge)
│   ├─ main.py                            # Python inference bridge (v10.11)
│   ├─ analysis.py                        # Post-cruise CSV analysis script
│   ├─ ngrok/                             # ngrok binary (excluded from git)
│   ├─ public/
│   │   └─ index.html                     # Dashboard frontend
│   └─ node_modules/                      # npm deps (excluded from git)
│
├─ Training/
│   ├─ Training.ipynb                     # Jupyter notebook for model dev
│   ├─ Training dataset/                  # 4 Excel files (Jati Six telemetry)
│   │   └─ Jati Six - April/May/June 2025.xlsx
│   └─ checkpoints_fyp(vessel_forecast)/
│       ├─ best_pinn.pt                   # PyTorch PINN weights (25 KB)
│       ├─ xgb.json                       # XGBoost model (2.3 MB)
│       ├─ xgb_model.onnx                 # ONNX export (940 KB)
│       ├─ scaler.pkl                     # sklearn StandardScaler (13 features)
│       ├─ physics_coef.pkl               # 7 physics coefficients
│       ├─ meta.pkl                       # training metadata (horizon, resample, etc.)
│       └─ co.py                          # ONNX conversion script
│
├─ sea trial/
│   ├─ Raw sensor(undecoded).csv          # 7.2 MB raw telemetry
│   ├─ sea trial 10aug (cleaned).xlsx     # cleaned trial data
│   ├─ sea trial log.xlsx                # trial log (3.4 MB)
│   ├─ FuelTrax.xlsx                     # fuel consumption log
│   ├─ *wind_vs_time.png                 # analysis charts
│   ├─ *wave_vs_time.png
│   └─ *actual_vs_predicted_vs_time.png
│
├─ Documentation/
│   ├─ 2212803_FYP_report.pdf            # Final year project report
│   ├─ article.doc                       # Draft writeup (18 MB)
│   ├─ Jati6_Sea_Trial_Report.docx       # Official sea-trial report
│   └─ Prediction_and_Monitoring_of_Vessel_Speed.pdf  # Research paper
│
├─ requirements.txt                      # Python dependencies
├─ .gitignore                            # Excludes node_modules, binaries, etc.
└─ README.md                             # This file
```

---

## How It Works

```
  ┌──────────────────────────────────────────────────────┐
  │  ESP32 Sensor Node  (on vessel)                     │
  │  ┌─ BNO08x IMU → roll/pitch/yaw + wave height Hs    │
  │  ├─ Airmar NMEA → wind, GPS, SOG                    │
  │  ├─ SD card → local backup logs                     │
  │  └─ WiFi + MQTT/TLS → cloud broker (HiveMQ)         │
  └────────────────────────────────────────┬────────────┘
                                           │  MQTT (TLS, port 8883)
                                           ▼
  ┌──────────────────────────────────────────────────────┐
  │  Python Inference Bridge  (laptop/raspberry pi)      │
  │  ├─ paho-mqtt: subscribes jati6/raw                 │
  │  ├─ WaveEstimator: 2nd-order high-pass + variance → Hs│
  │  ├─ ResampleAggregator: averages to training cadence  │
  │  ├─ Physics model: closed-form wind/wave → speed     │
  │  ├─ PINN: residual neural net (13-feature, LayerNorm)│
  │  ├─ XGBoost: blends physics + PINN → final Vp (kn)   │
  │  ├─ DashboardPusher: POST JSON to Node.js /ingest   │
  │  └─ LiveCSV + SessionHistory → CSV + PNG             │
  └────────────────────────────────────────┬────────────┘
                                           │  HTTP POST (JSON)
                                           ▼
  ┌──────────────────────────────────────────────────────┐
  │  Node.js Dashboard Server  (port 8080)               │
  │  ├─ Express: serves static dashboard.html           │
  │  ├─ POST /ingest: receives telemetry, broadcasts    │
  │  ├─ WebSocket: pushes live data to all browsers     │
  │  └─ /history, /healthz endpoints                     │
  └────────────────────────────────────────┬────────────┘
                                           │  WebSocket
                                           ▼
  ┌──────────────────────────────────────────────────────┐
  │  Browser Dashboard                                    │
  │  ├─ Real-time charts (Chart.js): SOG vs Vp, wind,     │
  │  │   wave, error, APE histogram                       │
  │  ├─ 3D scene (Three.js): live vessel attitude        │
  │  ├─ Map (Leaflet): live position                     │
  │  └─ Alert status: RED (<18 kn) / YELLOW / GREEN     │
  └──────────────────────────────────────────────────────┘
```

### Inference Model Architecture

- **Input features (13):** TRUE_WIND_KN, WAVE_HS_M, SOG_LAG1/2/3, SOG_ROLL5, SOG_STD5, SOG_DIFF1/3, HOUR_SIN/COS, MONTH_SIN/COS
- **Physics model:** 2nd-order polynomial in wind and wave, linear in lag-1 SOG (7 coefficients)
- **PINN:** 3 residual blocks (LayerNorm + SiLU), predicts speed residual
- **XGBoost:** takes [scaled features + physics output + PINN output] → final speed (kn)
- **Forecast horizon:** configured in `meta.pkl` (`forecast_horizon_s`)
- **Resample window:** configured in `meta.pkl` (`resample_seconds`)

---

## How to Replicate

### Prerequisites

- ESP32 development board with BNO08x IMU and Airmar NMEA output
- Arduino IDE (or PlatformIO) with ESP32 board package
- Python 3.10+
- Node.js 18+

### 1. Flash the ESP32 firmware

```bash
# In Arduino IDE:
#   Sketch → Include Library → Manage Libraries
#   Install: WiFiManager, PubSubClient, LittleFS_esp32, SparkFun BNO08x
#
#   Tools → Board → ESP32 Dev Module
#   Sketch → Upload
```

Edit `SCANS.ino` and set:
- `MQTT_HOST` / `MQTT_PORT` — your MQTT broker (or use the provided HiveMQ cluster)
- `MQTT_USER` / `MQTT_PASS` — create a dedicated credential (do NOT use the ESP32 default in production)
- `WIFI_AP_NAME` / `WIFI_AP_PASS` — WiFiManager AP name for initial setup

Connect GPIO0 to GND briefly during boot for WiFiManager captive-portal setup.

### 2. Set up the Python inference bridge

```bash
cd vessel-dashboard
pip install -r ../requirements.txt

# Create a .env or export env vars:
export JATI6_MQTT_USER="bridge_client"
export JATI6_MQTT_PASS="your-broker-password"
export JATI6_MODEL_DIR="/path/to/checkpoints_fyp(vessel_forecast)"
# optional — for remote dashboard:
export JATI6_DASHBOARD_URL="https://your-tunnel.trycloudflare.com/ingest"
```

> `main.py` defaults `JATI6_MODEL_DIR` to `C:\Users\fansuri\Documents\pro\fyp\checkpoints_fyp(vessel_forecast)`.
> Update that path or override with the env var above.

### 3. Start the dashboard server

```bash
cd vessel-dashboard
npm install
npm start    # or: node server.js  (listens on http://localhost:8080)
```

### 4. Start the inference bridge

```bash
cd vessel-dashboard
python main.py
```

The bridge will:
1. Connect to MQTT and wait for the ESP32 to come online
2. Calibrate the IMU baseline (8 s) and wind sensor (up to 40 s, needs calm conditions)
3. Enter the main loop: reads NMEA, runs inference at the configured cadence, pushes to dashboard

### 5. (Optional) Expose dashboard publicly

Run ngrok to share the dashboard URL:

```bash
# Edit start.bat to point NGROK_PATH to your ngrok.exe
# Or run manually:
ngrok http 8080
# Then set JATI6_DASHBOARD_URL to the ngrok https URL + /ingest
```

### 6. Post-cruise analysis

```bash
cd vessel-dashboard
python analysis.py
```

Edit the `CONFIG` block at the top of `analysis.py` to point at your live CSV and Jati 6 ground-truth XLSX, then run.

### Keyboard shortcut during bridge run

- Press **`r`** (console focused, Windows only) to trigger a soft-reset of sensors (IMU baseline + wave estimator recalibrate). The soft-reset hotkey auto-disables on macOS/Linux.

---

## MQTT Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `jati6/raw` | ESP32 → Python | Raw sensor lines (NMEA + IMU + wave) |
| `jati6/status` | ESP32 → Python | Online/offline LWT |
| `jati6/results` | Python → ESP32 | Inference results (SD-card mirror) |
| `jati6/results_header` | Python → ESP32 | CSV header (retained) for SD mirror |
| `jati6/command` | Python → ESP32 | `SOFT_RESET` / `FULL_RESET` |

---

## Configuration Reference

### Environment variables (Python bridge, `main.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `JATI6_MQTT_USER` | `ESP` | MQTT broker username (change from default!) |
| `JATI6_MQTT_PASS` | `12082003` | MQTT broker password (change from default!) |
| `JATI6_MODEL_DIR` | `C:\Users\fansuri\...` | Path to model checkpoint directory |
| `JATI6_DASHBOARD_URL` | `http://localhost:8080/ingest` | Dashboard ingest endpoint |

### Alert thresholds

- Green: Vp >= 20.0 kn
- Yellow: 18.0 <= Vp < 20.0 kn
- Red: Vp < 18.0 kn

---

## Notes & Caveats

- **Shared credentials:** Both the ESP32 (`SCANS.ino`) and the Python bridge default to the same MQTT credential (`ESP` / `12082003`). The code warns about this on startup — create separate broker credentials before deploying publicly.
- **Mock mode:** `SCANS.ino` has `MOCK_AIRMAR = true` by default for simulation/testing without real NMEA hardware.
- **`start.bat`** has a hardcoded ngrok path — edit `NGROK_PATH` if your ngrok is elsewhere.
- The **session plot** (`dashboard_plot.png`) is saved on every shutdown (Ctrl-C) and plots every 10 Hz loop tick.
- **`Training/Training.ipynb` is empty (0 bytes)** — it is a placeholder from early development and is non-essential. Model training was done in a separate environment; the committed checkpoints in `Training/checkpoints_fyp(vessel_forecast)/` are the final artifacts.
- **`deploy_to_github.py`** is a legacy PAT-based deploy script. Preferred method: use the `gh` CLI:
  ```bash
  gh repo create hafiz-fansuri/SCANS --public --source=. --push
  ```

---

## License

This is a final-year student project. See `Documentation/` for the full report.
