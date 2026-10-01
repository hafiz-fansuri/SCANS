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

## Setup Your Development Environment

> These steps assume you are starting from scratch on a fresh computer. No prior experience needed — follow them in order.

### 1. Install Arduino IDE (required for flashing the ESP32)

1. Go to https://www.arduino.cc/en/software
2. Download the **Windows** or **macOS** installer (64-bit).
3. Run the installer **as Administrator** and accept all defaults.
4. Launch Arduino IDE after installation completes.

#### Add the ESP32 board package:

1. In Arduino IDE: **File → Preferences**
2. In the field *"Additional Boards Manager URLs"* paste:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Click **OK**.
4. Go to **Tools → Board → Boards Manager…**
5. Search for `esp32` and install **esp32 by Espressif Systems**.
6. Select your board: **Tools → Board → ESP32 Dev Module**
7. Select the correct **Port** (Tools → Port → your ESP32, e.g. `COM3` on Windows or `/dev/ttyUSB0` on Linux/macOS).

#### Install required libraries (via Library Manager):

1. **Tools → Manage Libraries…** (wait for the index to load)
2. Search and install each of these (install the latest version of each):

   | Search term | Library name | Author |
   |-------------|-------------|--------|
   | `wifi` | WiFi (built-in, no install needed) | |
   | `WiFiManager` | WiFiManager | tzapu |
   | `PubSubClient` | PubSubClient | Nick O'Leary |
   | `LittleFS_esp32` | LittleFS_esp32 | lorol |
   | `SparkFun BNO08x` | SparkFun BNO08x Arduino Library | SparkFun |

### 2. Install VS Code (optional, for code editing)

> Arduino IDE is required for flashing. VS Code is a nicer editor for the Python/Node.js parts and is strongly recommended.

1. Go to https://code.visualstudio.com/
2. Download and install the Windows/macOS version.
3. (Recommended) Install these VS Code extensions:
   - **Python** (by Microsoft)
   - **PlatformIO IDE** (for ESP32 development, if you prefer PIO over Arduino IDE)
   - **GitLens** (for git integration)
   - **Prettier** (for automatic frontend formatting)

### 3. Install Python (3.10 or newer)

1. Go to https://www.python.org/downloads/
2. Download Python 3.12 (or latest stable).
3. **Important:** During installation, check the box *"Add Python to PATH"*.
4. After install, verify:
   ```cmd
   python --version
   pip --version
   ```

### 4. Install Node.js (18 or newer)

1. Go to https://nodejs.org/
2. Download the **LTS** version.
3. Run the installer with default settings.
4. Verify:
   ```cmd
   node --version
   npm --version
   ```

### 5. Create a Python virtual environment (recommended)

A virtual environment keeps all Python dependencies isolated in one folder so they don't conflict with other projects on your computer.

**Windows (Command Prompt / PowerShell):**
```cmd
cd SCANS\vessel-dashboard
python -m venv .venv
.venv\Scripts\activate
pip install -r ..\requirements.txt
```

**Windows (Git Bash) / macOS / Linux:**
```bash
cd SCANS/vessel-dashboard
python3 -m venv .venv
source .venv/bin/activate     # Windows Git Bash: .venv/Scripts/activate
pip install -r ../requirements.txt
```

You'll know it's working when you see `(.venv)` at the start of your prompt. When you come back to work later, just run `source .venv/bin/activate` again before working.

### 6. Install the dashboard npm dependencies

```bash
cd SCANS/vessel-dashboard
npm install
```

---

## Getting Started from GitHub

This repository is publicly hosted at: **https://github.com/hafiz-fansuri/SCANS**

### Step 1 — Clone the repository

Open a terminal (Command Prompt, PowerShell, or Git Bash) and run:

```bash
git clone https://github.com/hafiz-fansuri/SCANS.git
cd SCANS
```

### Step 2 — Set up the Python virtual environment and install dependencies

```bash
cd SCANS/vessel-dashboard

# Create and activate a virtual environment:
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate

# Install all Python dependencies:
pip install -r ../requirements.txt
```

> If `python` doesn't work, try `python3` instead.

### Step 3 — Configure environment variables

The Python inference bridge reads its settings from environment variables. Create a `.env` file in the `vessel-dashboard/` folder:

```bash
# Path to the model checkpoint directory (relative to this repo):
JATI6_MODEL_DIR=../Training/checkpoints_fyp(vessel_forecast)

# Your MQTT broker credentials:
# (Sign up for free at https://www.hivemq.com/cloud/ if you don't have one)
JATI6_MQTT_USER=your_broker_username
JATI6_MQTT_PASS=your_broker_password

# Optional — only set if the dashboard runs on a different machine:
JATI6_DASHBOARD_URL=http://localhost:8080/ingest
```

> Both the ESP32 firmware and the Python bridge ship with a default MQTT credential (`ESP` / `12082003`). The bridge logs a warning on startup if you haven't overridden this. Create separate credentials for each before deploying.

### Step 4 — Start the Node.js dashboard server

```bash
cd SCANS/vessel-dashboard
npm start
# Dashboard will be available at http://localhost:8080
```

Leave this running. Open a browser and go to http://localhost:8080 to see the dashboard.

### Step 5 — Start the Python inference bridge

Open a **new** terminal (don't close the dashboard server from Step 4):

```bash
cd SCANS/vessel-dashboard
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python main.py
```

The bridge will:
1. Connect to MQTT and wait for the ESP32 to come online
2. Calibrate the IMU baseline (8 seconds) and wind sensor (up to 40 seconds, needs calm conditions)
3. Enter the main loop — reads telemetry, runs the hybrid physics+PINN+XGBoost inference, and pushes live data to the dashboard

### Step 6 — Flash the ESP32 firmware (hardware step)

1. Connect your ESP32 to the computer via USB.
2. In Arduino IDE:
   - **File → Open** → navigate to `SCANS/ESP CODE/SCANS.ino`
   - Edit `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS` to match your broker
   - **Sketch → Verify/Compile** (no errors should appear)
   - **Sketch → Upload** (or click the right-arrow button)
3. Open the **Serial Monitor** (Tools → Serial Monitor, baud rate 115200) to watch boot messages.
4. On first boot: hold the **BOOT/IO0** button briefly to enter WiFiManager. Select your WiFi network and enter the password when the captive portal appears on your phone/laptop.

> The firmware ships with `MOCK_AIRMAR = true` by default. This generates simulated wind/SOG/GPS data so you can test the full pipeline without real NMEA hardware. Set it to `false` in `SCANS.ino` when you have a real Airmar connected.

### Step 7 — Run post-cruise analysis (after a voyage)

After collecting data, run the analysis script to merge the live CSV with ground-truth data:

```bash
cd SCANS/vessel-dashboard
source .venv/bin/activate
python analysis.py
```

Edit the `CONFIG` block at the top of `analysis.py` to point at your live CSV and ground-truth XLSX files.

### Step 8 — Retrain the model (optional)

```bash
pip install jupyter
jupyter notebook
# Open Training/Training.ipynb in the notebook interface
```

### Step 9 — Push changes back to GitHub

```bash
cd SCANS
git add -A
git commit -m "your descriptive message"
git push origin main
```

---

## Replicate the Full Project (Beginner Guide)

Here is the complete sequence from zero to a running system, assuming you have no software installed yet.

### Overview

You will: (1) install software, (2) clone the repo, (3) flash the ESP32, (4) connect to WiFi, (5) set up MQTT, (6) run the dashboard + inference bridge, (7) verify everything works.

### Step-by-step

**A. Install all software** — Follow the [Setup Your Development Environment](#setup-your-development-environment) section above:
1. Arduino IDE + ESP32 board + libraries
2. VS Code (optional)
3. Python 3.10+
4. Node.js 18+

**B. Clone the repo**
```bash
git clone https://github.com/hafiz-fansuri/SCANS.git
cd SCANS
```

**C. Set up Python venv + install dependencies**
```bash
cd SCANS/vessel-dashboard
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r ../requirements.txt
```

**D. Set up the Node.js dashboard**
```bash
cd SCANS/vessel-dashboard
npm install
npm start
# Leave this running — the dashboard is now at http://localhost:8080
```

**E. Create an MQTT broker account (free)**
1. Go to https://www.hivemq.com/cloud/
2. Sign up for a free account
3. In the HiveMQ Cloud Console, create a new cluster and note the **cluster host URL** and **port (8883)**
4. Create a new MQTT credential (username + password) — use something unique, not the default `ESP`/`12082003`

**F. Flash the ESP32 firmware**
1. Open `ESP CODE/SCANS.ino` in Arduino IDE
2. Change these values in the code:
   ```cpp
   const char* MQTT_HOST = "your-cluster-host.hivemq.cloud";
   const int   MQTT_PORT = 8883;
   const char* MQTT_USER = "your_username";
   const char* MQTT_PASS = "your_password";
   ```
3. Upload the sketch to your ESP32
4. Open Serial Monitor (115200 baud)
5. On first boot, hold the **BOOT** button to enter WiFiManager
6. Connect to the WiFi network named `Jati9-Setup` with password `123456789`
7. In the captive portal, select your vessel's WiFi network and enter its password

**G. Configure the Python bridge**
Create a `.env` file in `vessel-dashboard/`:
```bash
JATI6_MODEL_DIR=../Training/checkpoints_fyp(vessel_forecast)
JATI6_MQTT_USER=your_username
JATI6_MQTT_PASS=your_password
```

> If your ESP32 firmware's `MQTT_HOST` doesn't match the default in `main.py`, also set:
> ```bash
> JATI6_MQTT_HOST=your-cluster-host.hivemq.cloud
> ```

**H. Start the inference bridge**
```bash
cd vessel-dashboard
source .venv/bin/activate
python main.py
```

Watch the console — you should see:
```
MQTT connected to broker
IMU baseline=...
Wind calib: ...
─── All models loaded ────────────────────────────────────
Entering main loop — Ctrl-C to stop
```

If you see MQTT data lines appearing and the dashboard updating, everything is working.

**I. Verify via the dashboard**
Open http://localhost:8080 in a browser. You should see:
- Live SOG, wind speed, and wave height values
- Vp forecast speed (kn)
- Alert status indicator (RED / YELLOW / GREEN)
- A live-updating chart

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
Forecast horizon: configured in `meta.pkl` (`forecast_horizon_s`)
Resample window: configured in `meta.pkl` (`resample_seconds`)

---

## MQTT Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `jati6/raw` | ESP32 → Python | Raw sensor lines (NMEA + IMU + wave) |
| `jati6/status` | ESP32 → Python | Online/offline Last-Will |
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

- **Shared credentials:** Neither the ESP32 (`SCANS.ino`) nor the Python bridge override the MQTT credential (`ESP` / `12082003`) at startup — create separate broker credentials before deploying publicly.
- **Mock mode:** `SCANS.ino` has `MOCK_AIRMAR = true` by default for simulation/testing without real NMEA hardware. Set it to `false` when you have a real Airmar NMEA device connected.
- **`start.bat`** has a hardcoded ngrok path — edit `NGROK_PATH` if your ngrok is elsewhere.
- The **session plot** (`dashboard_plot.png`) is saved on every shutdown (Ctrl-C) and plots every 10 Hz loop tick.
- **`Training/Training.ipynb`** contains the full model training pipeline — Excel/CSV ingest, physics-informed residual modeling, PINN + XGBoost training, and checkpoint export. The model artifacts in `Training/checkpoints_fyp(vessel_forecast)/` are the committed outputs of this notebook.
- **`deploy_to_github.py`** is a legacy PAT-based deploy helper. Preferred method: use the `gh` CLI:
  ```bash
  gh repo create hafiz-fansuri/SCANS --public --source=. --push
  ```
  Or the HTTPS remote manually:
  ```bash
  git remote add origin https://github.com/hafiz-fansuri/SCANS.git
  git branch -M main
  git push -u origin main
  ```

---

## License

This is a final-year student project. See `Documentation/` for the full report.
