"""
=============================================================
  GREY-BOX DASHBOARD BRIDGE  —  v10.11  (plot at full logging rate)
  Author : fansuri

  CHANGES vs v10.10:
  ─────────────────────────────────────────────────────────
  1. Session PNG now plots EVERY loop tick (LOOP_HZ, 10 Hz), the
     same rows that go into the LiveCSV, instead of one point per
     aggregated forecast (every resample_s). history.push() moved
     out of the forecast block into the per-tick section, right
     after live_csv.append().
  2. SessionHistory maxlen raised 50_000 -> 500_000 (~13.9 h at
     10 Hz) so a long session doesn't silently drop its start.
  3. Time axis is still formatted hh:mm, start -> shutdown.

  Note: the APE histogram now counts ticks, not forecasts. The
  console AvgAPE is still computed once per forecast, so the two
  numbers won't match exactly.

  Everything else — MQTT transport, ResampleAggregator, PINN,
  XGBoost stack, IMU reader, LagBuffer, wave estimator, stationary
  reset, soft-reset hotkey, v10.7 inference-failure recovery,
  v10.10 lat/lon in payload + CSV — unchanged.

  REQUIRES: pip install paho-mqtt
  NOTE: msvcrt is Windows-only (stdlib). The hotkey feature will
  auto-disable itself on macOS/Linux.
=============================================================
"""

import os
import csv
import json
import math
import time
import queue
import logging
import threading
import collections
from datetime import datetime, timedelta

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import requests
import torch
import torch.nn as nn
import xgboost as xgb
import joblib
import paho.mqtt.client as mqtt

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
MQTT_HOST = "d97c13f07e614f59a775fa47312851ad.s1.eu.hivemq.cloud"   # ← same cluster host as the ESP32
MQTT_PORT = 8883                             # TLS

# Credentials: prefer env vars, fall back to the previous hardcoded
# values so nothing breaks if you haven't set them up yet. Set these
# before committing this repo anywhere public:
#   export JATI6_MQTT_USER="bridge_client"
#   export JATI6_MQTT_PASS="<something-not-the-esp32s-password>"
MQTT_USER = os.environ.get("JATI6_MQTT_USER", "ESP")
MQTT_PASS = os.environ.get("JATI6_MQTT_PASS", "12082003")

TOPIC_DATA    = "jati6/raw"
TOPIC_STATUS  = "jati6/status"
TOPIC_RESULTS = "jati6/results"     # inference results published back to the ESP32
TOPIC_RESULTS_HEADER = "jati6/results_header"   # retained header for the ESP32's SD mirror
TOPIC_COMMAND = "jati6/command"     # remote command channel (e.g. SOFT_RESET)
# NOTE: MQTT_USER/MQTT_PASS above default to the SAME credential the
# ESP32 firmware uses. That's a real gap, not just a comment — create
# a separate broker credential for this bridge and set JATI6_MQTT_USER
# / JATI6_MQTT_PASS before this goes anywhere the ESP32's firmware
# source (which has its own hardcoded copy) might also be visible.

DASHBOARD_URL = os.environ.get("JATI6_DASHBOARD_URL", "http://localhost:8080/ingest")
# If the dashboard server is remote (Cloudflare Tunnel / Render / etc),
# set JATI6_DASHBOARD_URL instead, e.g.:
#   export JATI6_DASHBOARD_URL="https://your-tunnel-subdomain.trycloudflare.com/ingest"

# ── points at the v7.5 FORECASTING checkpoint dir ──────────────
MODEL_DIR = os.environ.get(
    "JATI6_MODEL_DIR",
    r"C:\Users\fansuri\Documents\pro\fyp\checkpoints_fyp(vessel_forecast)"
)

LOOP_HZ      = 10             # live telemetry / dashboard / LiveCSV / plot rate
CONSOLE_PRINT_HZ = 2.0         # how often a status line prints to the console
session_id = datetime.now().strftime("%Y%m%d")
LIVE_LOG_PATH = f"vessel_live_log_{session_id}.csv"
LOG_MAXROWS  = 2_000

THRESH_GREEN  = 20.0          # kn
THRESH_YELLOW = 18.0          # kn

# Numeric status code — still used by the ESP32 SD-mirror? No: as of v10.8
# the CSV / SD mirror carry the text label directly (RED / YELLOW / GREEN).
# Kept only so the dashboard payload / any external tool importing it
# doesn't break.
STATUS_CODE = {"RED": 0, "YELLOW": 1, "GREEN": 2}

# Set to a float (e.g. 18.5) to simulate SOG indoors for demo/testing
MOCK_SOG = None

WIND_CALIB_SAMPLES = 30
WIND_CALIB_TIMEOUT = 40.0

# Set False to disable the soft-reset hotkey entirely. When False,
# no watcher thread is started, msvcrt is never imported, and the
# script behaves exactly like the pre-hotkey version.
ENABLE_SOFT_RESET_HOTKEY = True

# ── Wave estimator — tuned for SEA-SCALE motion ───────────────
WAVE_HP_FC    = 0.06     # 0.06Hz (~16.7s period) lets real swell/chop (3-10s) through
                          # while still rejecting slow sensor drift.
WAVE_HP_FC2   = 0.05     # second-stage filter, same reasoning
WAVE_VARIANCE_TAU_RISE_S = 10.0   # slow — resists over-reacting to a single spike
WAVE_VARIANCE_TAU_FALL_S = 2.0    # fast — this is what actually fixes the "gradually
                                   # goes down instead of settling" symptom
WAVE_HS_ZERO_FLOOR_M = 0.02       # below this, report exact 0.0 instead of residual jitter
WAVE_WARMUP   = 100      # warmup samples before Hs is reported
WAVE_ROAD_MAX = 3.0      # ceiling in metres for the sea state expected at trial site
                          # (river/strait/nearshore chop, not open ocean)
                          # NOTE: if you're running this against ROAD test data instead
                          # of SEA data, these four constants need to match whichever
                          # regime the currently-loaded MODEL_DIR checkpoint was trained
                          # on — a mismatch degrades Vp silently, no error thrown.

# ── stationary reset (ZUPT-style) ──────────────────────────────
STATIONARY_SOG_THRESH_KN = 0.3
STATIONARY_RESET_S       = 5.0

KN_TO_KMH    = 1.852
MAX_SPEED_KN = 35.0

FEATURE_COLS = [
    "TRUE_WIND_KN", "WAVE_HS_M",
    "SOG_LAG1", "SOG_LAG2", "SOG_LAG3",
    "SOG_ROLL5", "SOG_STD5", "SOG_DIFF1", "SOG_DIFF3",
    "HOUR_SIN", "HOUR_COS", "MONTH_SIN", "MONTH_COS",
]

# ── inference-failure recovery (v10.7) ─────────────────────────
MAX_CONSECUTIVE_FAILURES = 3   # after this many in a row, log.critical() so it's
                                # impossible to miss in the console/log file

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("matplotlib").setLevel(logging.WARNING)

if MQTT_USER == "ESP" and MQTT_PASS == "12082003":
    log.warning("  Using default/shared MQTT credentials (same as ESP32 firmware). "
                "Set JATI6_MQTT_USER / JATI6_MQTT_PASS to a separate credential "
                "before this repo goes anywhere public.")

# conditional import: only pull in msvcrt if the hotkey is actually
# enabled, and auto-disable gracefully if it's unavailable (e.g.
# running on macOS/Linux).
if ENABLE_SOFT_RESET_HOTKEY:
    try:
        import msvcrt
    except ImportError:
        log.warning("  msvcrt unavailable (not Windows) — disabling soft-reset hotkey")
        ENABLE_SOFT_RESET_HOTKEY = False

PINN_PATH    = os.path.join(MODEL_DIR, "best_pinn.pt")
XGB_PATH     = os.path.join(MODEL_DIR, "xgb.json")
SCALER_PATH  = os.path.join(MODEL_DIR, "scaler.pkl")
PHYSICS_PATH = os.path.join(MODEL_DIR, "physics_coef.pkl")
META_PATH    = os.path.join(MODEL_DIR, "meta.pkl")


# ══════════════════════════════════════════════════════════════
#  MQTT READER
# ══════════════════════════════════════════════════════════════
class MqttReader:
    def __init__(self, host, port, user, password, data_topic, status_topic, results_topic, results_header_topic):
        self._q = queue.Queue(maxsize=5000)
        self.connected = threading.Event()
        self.vessel_online = threading.Event()

        self._client = mqtt.Client(client_id="jati6-bridge", protocol=mqtt.MQTTv311)
        self._client.username_pw_set(user, password)
        self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_log = lambda client, userdata, level, buf: log.debug(f"MQTT: {buf}")
        self._data_topic = data_topic
        self._status_topic = status_topic
        self._results_topic = results_topic
        self._results_header_topic = results_header_topic

        self._client.connect_async(host, port, keepalive=15)
        self._client.reconnect_delay_set(min_delay=1, max_delay=5)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("  MQTT connected to broker")
            client.subscribe(self._data_topic, qos=1)
            client.subscribe(self._status_topic, qos=1)
            self.connected.set()
        else:
            log.warning(f"  MQTT connect failed, rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected.clear()
        log.warning(f"  MQTT disconnected (rc={rc}) — paho will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        if msg.topic == self._status_topic:
            status = msg.payload.decode(errors="ignore")
            if status == "online":
                self.vessel_online.set()
                log.info("  Vessel (ESP32) reports ONLINE")
            else:
                self.vessel_online.clear()
                log.warning("  Vessel (ESP32) reports OFFLINE (last-will fired)")
            return

        line = msg.payload.decode(errors="ignore").strip()
        if not line:
            return
        if line.startswith("T") and "|" in line:
            head, _, rest = line.partition("|")
            try:
                int(head[1:])
                line = rest
            except ValueError:
                pass
        try:
            self._q.put_nowait(line)
        except queue.Full:
            pass

    def readline(self, timeout=1.0):
        try:
            line = self._q.get(timeout=timeout)
            return (line + "\n").encode()
        except queue.Empty:
            return b""

    def publish_result(self, line: str):
        try:
            self._client.publish(self._results_topic, line, qos=1)
        except Exception as e:
            log.debug(f"  Result publish error: {e}")

    def publish_header(self, header_line: str):
        try:
            self._client.publish(self._results_header_topic, header_line, qos=1, retain=True)
            log.info(f"  Published SD-card CSV header (retained) → {self._results_header_topic}")
        except Exception as e:
            log.debug(f"  Header publish error: {e}")

    def publish_command(self, cmd: str):
        try:
            self._client.publish(TOPIC_COMMAND, cmd, qos=1)
            log.info(f"  Sent command → {cmd}")
        except Exception as e:
            log.debug(f"  Command publish error: {e}")

    def close(self):
        self._client.loop_stop()
        self._client.disconnect()


# ══════════════════════════════════════════════════════════════
#  HOTKEY WATCHER
# ══════════════════════════════════════════════════════════════
class HotkeyWatcher:
    def __init__(self, key: str = "r"):
        self.key = key.lower()
        self.triggered = threading.Event()
        threading.Thread(target=self._watch, daemon=True).start()
        log.info(f"  Hotkey armed — press '{self.key}' (console focused) to soft-reset sensors")

    def _watch(self):
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getch().decode(errors="ignore").lower()
                if ch == self.key:
                    self.triggered.set()
            time.sleep(0.05)


# ══════════════════════════════════════════════════════════════
#  RESAMPLE AGGREGATOR
# ══════════════════════════════════════════════════════════════
class ResampleAggregator:
    def __init__(self, window_seconds: float):
        self.window_seconds = window_seconds
        self._wind_samples = []
        self._sog_samples  = []
        self._wave_samples = []
        self._window_start = time.time()

    def push(self, wind_kn, sog_kn, wave_hs_m):
        self._wind_samples.append(wind_kn)
        self._sog_samples.append(sog_kn)
        self._wave_samples.append(wave_hs_m)

    def ready(self) -> bool:
        return (time.time() - self._window_start) >= self.window_seconds

    def flush(self):
        """Returns (wind_kn, sog_kn, wave_hs_m) means for the elapsed
        window, or None if no samples arrived — resets the window
        either way so it doesn't drift."""
        self._window_start = time.time()
        if not self._sog_samples:
            return None
        result = (
            float(np.mean(self._wind_samples)) if self._wind_samples else 0.0,
            float(np.mean(self._sog_samples)),
            float(np.mean(self._wave_samples)) if self._wave_samples else 0.0,
        )
        self._wind_samples.clear()
        self._sog_samples.clear()
        self._wave_samples.clear()
        return result


# ══════════════════════════════════════════════════════════════
#  PINN
# ══════════════════════════════════════════════════════════════
class ResBlock(nn.Module):
    def __init__(self, dim, dropout=0.0):
        super().__init__()
        self.block = nn.Sequential(
            nn.LayerNorm(dim), nn.Linear(dim, dim), nn.SiLU(),
            nn.Dropout(dropout), nn.Linear(dim, dim),
        )
    def forward(self, x):
        return x + self.block(x)

class PINN(nn.Module):
    def __init__(self, in_dim, hidden=64, n_layers=3, dropout=0.0):
        super().__init__()
        self.input_proj = nn.Sequential(nn.Linear(in_dim, hidden), nn.SiLU(), nn.Dropout(dropout))
        self.res_blocks = nn.Sequential(*[ResBlock(hidden, dropout) for _ in range(n_layers)])
        self.head = nn.Linear(hidden, 1)
    def forward(self, x):
        return self.head(self.res_blocks(self.input_proj(x)))


def load_models():
    log.info("─── Loading models ───────────────────────────────────")
    scaler = joblib.load(SCALER_PATH)
    log.info(f"  Scaler loaded — expects {scaler.n_features_in_} features")
    if scaler.n_features_in_ != 13:
        raise SystemExit(1)

    meta = joblib.load(META_PATH)
    res_mean = float(meta["res_mean"]); res_std = float(meta["res_std"])
    alpha = float(meta.get("res_shrink_alpha", 1.0))
    horizon_s = int(meta.get("forecast_horizon_s", 0))
    resample_s = int(meta.get("resample_seconds", 0))
    log.info(f"  meta.pkl — res_mean={res_mean:.4f}  res_std={res_std:.4f}  alpha={alpha:.4f}")
    log.info(f"  meta.pkl — forecast_horizon_s={horizon_s}s  resample_seconds={resample_s}s")
    if horizon_s == 0:
        log.warning("  meta.pkl has forecast_horizon_s=0 — this is a NOWCAST model, "
                     "not a forecast. Confirm MODEL_DIR points at the right checkpoint.")
    if resample_s == 0:
        log.warning("  meta.pkl has resample_seconds=0 — the ResampleAggregator below "
                     "will do nothing useful. Confirm MODEL_DIR points at the v7.5+ "
                     "checkpoint trained with uniform cadence resampling.")

    phys_coef = joblib.load(PHYSICS_PATH)
    log.info(f"  Physics coef ({len(phys_coef)}): {np.round(phys_coef, 4)}")
    if len(phys_coef) != 7:
        raise SystemExit(1)

    state = torch.load(PINN_PATH, map_location="cpu", weights_only=True)
    in_dim = state["input_proj.0.weight"].shape[1]
    hidden = state["input_proj.0.weight"].shape[0]
    n_layers = sum(1 for k in state if k.startswith("res_blocks.") and k.endswith(".block.1.weight"))
    if in_dim != 13:
        raise SystemExit(1)
    pinn = PINN(in_dim=in_dim, hidden=hidden, n_layers=n_layers, dropout=0.0)
    pinn.load_state_dict(state); pinn.eval()
    log.info(f"  PINN loaded — in_dim={in_dim} hidden={hidden} layers={n_layers}")

    xgb_model = xgb.Booster()
    xgb_model.load_model(XGB_PATH)
    log.info("  XGBoost loaded OK")

    vp_test = infer(5.0, 0.08, 12.0, _DummyLag(12.0), scaler, pinn, xgb_model,
                     phys_coef, res_mean, res_std, alpha)
    log.info(f"  Self-test OK — forecast Vp = {vp_test:.3f} kn "
             f"({horizon_s}s ahead of the test inputs)")
    log.info("─── All models loaded ────────────────────────────────")
    return scaler, pinn, xgb_model, phys_coef, res_mean, res_std, alpha, horizon_s, resample_s


class _DummyLag:
    def __init__(self, val):
        self.lag1 = val; self.lag2 = val; self.lag3 = val
        self.roll5 = val; self.std5 = 0.0; self.diff1 = 0.0; self.diff3 = 0.0


# ══════════════════════════════════════════════════════════════
#  NMEA HELPERS
# ══════════════════════════════════════════════════════════════
def nmea_field(sentence, n):
    parts = sentence.split(",")
    if n >= len(parts):
        return ""
    val = parts[n]; star = val.find("*")
    return val[:star].strip() if star >= 0 else val.strip()

def parse_wimwv(sentence):
    status = nmea_field(sentence, 5)
    if status != "A":
        return None
    try:
        speed = float(nmea_field(sentence, 3))
        unit = nmea_field(sentence, 4)
        if unit in ("K", "k"): speed *= 0.539957
        elif unit in ("M", "m"): speed *= 1.94384
        return max(0.0, speed)
    except ValueError:
        return None

def parse_gpgga(sentence):
    try:
        fix = int(nmea_field(sentence, 6)) if nmea_field(sentence, 6) else 0
        sats = int(nmea_field(sentence, 7)) if nmea_field(sentence, 7) else 0
        return fix, sats
    except (ValueError, IndexError):
        return 0, 0

def parse_sog(sentence):
    prefix = sentence[:6].upper()
    if prefix in ("$GPRMC", "$GNRMC"):
        if nmea_field(sentence, 2) != "A":
            return None
        try:
            sog = float(nmea_field(sentence, 7))
            return sog if 0.0 <= sog < 50.0 else None
        except ValueError:
            return None
    if prefix in ("$GPVTG", "$GNVTG", "$IIVTG"):
        try:
            sog = float(nmea_field(sentence, 5))
            return sog if 0.0 <= sog < 50.0 else None
        except ValueError:
            return None
    return None

def is_boot_noise(line):
    low = line.lower()
    return any(t in low for t in ("rst:", "ets ", "boot:", "configsip", "load:", "entry "))


# ── GPS-derived SOG fallback (haversine between consecutive GPGGA fixes) ──
RMC_VTG_STALE_S      = 5.0    # if no RMC/VTG for this long, trust GPGGA-derived SOG instead
GPS_FALLBACK_MIN_DT  = 0.5    # ignore fixes closer together than this (GPS jitter)
GPS_FALLBACK_MAX_KN  = 50.0   # reject implausible jumps (bad fix / multipath glitch)


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def parse_gpgga_latlon(sentence):
    """Returns (lat, lon) in decimal degrees, or None if no valid fix."""
    fix = nmea_field(sentence, 6)
    if not fix or int(fix) == 0:
        return None
    try:
        lat_raw = nmea_field(sentence, 2)   # ddmm.mmmm
        lat_dir = nmea_field(sentence, 3)
        lon_raw = nmea_field(sentence, 4)   # dddmm.mmmm
        lon_dir = nmea_field(sentence, 5)
        if not lat_raw or not lon_raw:
            return None
        lat = float(lat_raw[:2]) + float(lat_raw[2:]) / 60.0
        if lat_dir == "S":
            lat = -lat
        lon = float(lon_raw[:3]) + float(lon_raw[3:]) / 60.0
        if lon_dir == "W":
            lon = -lon
        return lat, lon
    except (ValueError, IndexError):
        return None


class GpsSogEstimator:
    """Derives SOG (kn) from consecutive $GPGGA fixes via haversine
    distance/dt. Fallback for when RMC/VTG aren't coming through
    (e.g. the v6.6 buffer-overrun bug slicing them mid-sentence)."""
    def __init__(self):
        self._last_lat = self._last_lon = self._last_t = None
        self.sog_kn = 0.0

    def update(self, lat, lon, t):
        if self._last_lat is None:
            self._last_lat, self._last_lon, self._last_t = lat, lon, t
            return self.sog_kn
        dt = t - self._last_t
        if dt < GPS_FALLBACK_MIN_DT:
            return self.sog_kn
        dist_m = haversine_m(self._last_lat, self._last_lon, lat, lon)
        speed_kn = (dist_m / dt) * 1.94384
        self._last_lat, self._last_lon, self._last_t = lat, lon, t
        if speed_kn > GPS_FALLBACK_MAX_KN:
            return self.sog_kn   # reject implausible jump
        self.sog_kn = speed_kn
        return self.sog_kn


# ══════════════════════════════════════════════════════════════
#  WAVE ESTIMATOR
# ══════════════════════════════════════════════════════════════
class WaveEstimator:
    def __init__(self):
        self.tau_rise = WAVE_VARIANCE_TAU_RISE_S
        self.tau_fall = WAVE_VARIANCE_TAU_FALL_S
        self.hp1_in = self.hp1_out = 0.0
        self.vel = 0.0
        self.hp2_in = self.hp2_out = 0.0
        self.disp = 0.0
        self.wm = self.ws = 0.0
        self.n = 0
        self.hs = 0.0
        self._last_t = None

    def update(self, az, t):
        if self._last_t is None:
            self._last_t = t
            return self.hs
        dt = t - self._last_t
        self._last_t = t
        if dt <= 0.0 or dt > 0.5:
            return self.hs

        a1 = (1.0 / (2*math.pi*WAVE_HP_FC)) / (1.0 / (2*math.pi*WAVE_HP_FC) + dt)
        a2 = (1.0 / (2*math.pi*WAVE_HP_FC2)) / (1.0 / (2*math.pi*WAVE_HP_FC2) + dt)

        az_hp = a1 * (self.hp1_out + az - self.hp1_in)
        self.hp1_in, self.hp1_out = az, az_hp
        self.vel += az_hp * dt
        vel_hp = a2 * (self.hp2_out + self.vel - self.hp2_in)
        self.hp2_in, self.hp2_out = self.vel, vel_hp
        self.disp += vel_hp * dt

        self.n += 1
        delta = self.disp - self.wm
        inst_var = delta * delta
        tau = self.tau_rise if inst_var > self.ws else self.tau_fall
        alpha_w = math.exp(-dt / tau)
        self.wm = alpha_w * self.wm + (1 - alpha_w) * self.disp
        delta2 = self.disp - self.wm
        self.ws = alpha_w * self.ws + (1 - alpha_w) * delta * delta2

        if self.n > WAVE_WARMUP:
            hs = 4.0 * math.sqrt(max(self.ws, 0.0))
            hs = max(0.0, min(hs, WAVE_ROAD_MAX))
            self.hs = hs if hs > WAVE_HS_ZERO_FLOOR_M else 0.0
        return self.hs

    def stationary_reset(self):
        self.disp = 0.0
        self.vel = 0.0
        self.wm = 0.0
        self.ws = 0.0
        self.hs = 0.0


# ══════════════════════════════════════════════════════════════
#  IMU READER
# ══════════════════════════════════════════════════════════════
class ImuReader:
    def __init__(self):
        self._baseline = 0.0
        self._read_count = 0
        self.last_roll = 0.0
        self.last_pitch = 0.0
        self.last_yaw = 0.0

    def calibrate(self, ser, n=300, timeout=8.0):
        log.info("Calibrating IMU baseline (world-frame az) ...")
        samples = []
        t0 = time.time()
        while len(samples) < n and time.time() - t0 < timeout:
            line = ser.readline().decode(errors="ignore").strip()
            parsed = self._parse(line)
            if parsed is not None:
                samples.append(parsed[0])
        self._baseline = float(np.mean(samples)) if samples else 0.0
        log.info(f"  IMU baseline={self._baseline:.4f} m/s²  ({len(samples)} samples)")
        if not samples:
            log.warning("  IMU calib: 0 samples — check ESP32 is online and publishing")

    def read(self, line):
        parsed = self._parse(line)
        if parsed is None:
            return None
        az, roll, pitch, yaw = parsed
        self.last_roll, self.last_pitch, self.last_yaw = roll, pitch, yaw
        corrected = az - self._baseline
        self._read_count += 1
        if self._read_count % 200 == 0:
            log.debug(f"  IMU #{self._read_count}: az={corrected:.4f} "
                      f"roll={roll:.1f} pitch={pitch:.1f} yaw={yaw:.1f}")
        return corrected

    def _parse(self, line):
        if line.startswith("IMU:"):
            try:
                parts = line[4:].split(",")
                az = float(parts[0])
                roll = float(parts[1]) if len(parts) > 1 else 0.0
                pitch = float(parts[2]) if len(parts) > 2 else 0.0
                yaw = float(parts[3]) if len(parts) > 3 else 0.0
                return az, roll, pitch, yaw
            except (ValueError, IndexError):
                pass
        return None


# ══════════════════════════════════════════════════════════════
#  WIND CALIBRATION
# ══════════════════════════════════════════════════════════════
def calibrate_wind(ser):
    log.info("Calibrating wind baseline ...")
    samples = []
    t0 = time.time()
    lines_seen = wimwv_seen = wimwv_void = wimwv_valid = 0
    while len(samples) < WIND_CALIB_SAMPLES and time.time() - t0 < WIND_CALIB_TIMEOUT:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        lines_seen += 1
        if not line.startswith("$WIMWV"):
            continue
        wimwv_seen += 1
        spd = parse_wimwv(line)
        if spd is None:
            wimwv_void += 1
            continue
        if spd >= 10.0:
            continue
        wimwv_valid += 1
        samples.append(spd)
    baseline = float(np.mean(samples)) if samples else 0.0
    log.info(f"  Wind calib: lines={lines_seen} $WIMWV={wimwv_seen} "
             f"void={wimwv_void} valid={wimwv_valid} baseline={baseline:.3f}kn")
    if wimwv_seen == 0:
        log.warning("  !! No $WIMWV received — check ESP32 is online and publishing !!")
    return baseline


# ══════════════════════════════════════════════════════════════
#  LAG BUFFER
# ══════════════════════════════════════════════════════════════
class LagBuffer:
    def __init__(self, maxlen=10):
        self._buf = collections.deque([0.0] * maxlen, maxlen=maxlen)
    def push(self, sog: float):
        self._buf.append(sog)
    def _get(self, idx):
        lst = list(self._buf)
        try:
            return lst[-(idx + 1)]
        except IndexError:
            return lst[0]
    @property
    def current(self): return self._get(0)
    @property
    def lag1(self): return self._get(1)
    @property
    def lag2(self): return self._get(2)
    @property
    def lag3(self): return float(np.mean([self._get(i) for i in range(1, 4)]))
    @property
    def roll5(self): return float(np.mean([self._get(i) for i in range(5)]))
    @property
    def std5(self): return float(np.std([self._get(i) for i in range(5)]))
    @property
    def diff1(self): return self.current - self.lag1
    @property
    def diff3(self): return self.current - self._get(3)


# ══════════════════════════════════════════════════════════════
#  PHYSICS + INFERENCE
# ══════════════════════════════════════════════════════════════
def physics_predict(wind_true_kn, wave_hs_m, lag1_kn, coef):
    phi = np.array([1.0, wind_true_kn, wave_hs_m, wind_true_kn**2, wave_hs_m**2,
                     wind_true_kn*wave_hs_m, lag1_kn], dtype=np.float64)
    return float(np.clip(phi @ coef, 0.0, MAX_SPEED_KN))


@torch.no_grad()
def infer(wind_app_kn, wave_hs_m, sog_kn, lag_buf, scaler, pinn, xgb_model,
          phys_coef, res_mean, res_std, alpha=1.0):
    now = datetime.now()
    hour = now.hour + now.minute / 60.0
    month = now.month
    hour_sin = math.sin(2*math.pi*hour/24.0); hour_cos = math.cos(2*math.pi*hour/24.0)
    month_sin = math.sin(2*math.pi*(month-1)/12.0); month_cos = math.cos(2*math.pi*(month-1)/12.0)
    true_wind_kn = max(0.0, wind_app_kn - sog_kn)

    x_raw = np.array([[true_wind_kn, wave_hs_m, lag_buf.lag1, lag_buf.lag2, lag_buf.lag3,
                        lag_buf.roll5, lag_buf.std5, lag_buf.diff1, lag_buf.diff3,
                        hour_sin, hour_cos, month_sin, month_cos]], dtype=np.float32)

    # ── v10.7: validate BEFORE it reaches scaler/pinn/xgboost ──
    # A NaN/Inf here would previously surface as some opaque exception
    # three layers down (sklearn, torch, or xgboost's own error), caught
    # by the bare except in main() and silently converted into a frozen
    # forecast. Catch it here instead, and name exactly which feature
    # is bad, so a failure is diagnosable from the log line alone.
    if not np.all(np.isfinite(x_raw)):
        bad_idx = np.where(~np.isfinite(x_raw[0]))[0]
        bad_names = [FEATURE_COLS[i] for i in bad_idx]
        raise ValueError(
            f"Non-finite feature value(s) before scaling: {bad_names} = "
            f"{[x_raw[0][i] for i in bad_idx]} (full row: {x_raw[0].tolist()})"
        )

    x_scaled = scaler.transform(x_raw)

    vphys = physics_predict(true_wind_kn, wave_hs_m, lag_buf.lag1, phys_coef)
    res_n = pinn(torch.from_numpy(x_scaled).float()).item()
    res_kn = res_n * res_std + res_mean
    pinn_out = float(np.clip(vphys + alpha * res_kn, 0.0, MAX_SPEED_KN))

    x_xgb = np.concatenate([x_scaled[0], [vphys, pinn_out]]).reshape(1, -1)
    vp = float(np.clip(xgb_model.predict(xgb.DMatrix(x_xgb.astype(np.float32)))[0], 0.0, MAX_SPEED_KN))
    return vp


def compute_pct_error(sog_kn: float, vp_kn: float) -> float:
    denom = max(abs(sog_kn), 0.5)
    return abs(sog_kn - vp_kn) / denom * 100.0


def print_status_line(wind_kn, wave_cm, sog_kn, vp_kn, horizon_s, gps_fix, gps_sats,
                       loop_n, pct, avg_ape, forecast_source="ml"):
    fix_str = "FIX" if gps_fix > 0 else "NOFIX"
    horizon_note = f"+{horizon_s}s" if horizon_s > 0 else "now"
    degraded_tag = "" if forecast_source == "ml" else f"  [DEGRADED:{forecast_source}]"
    log.info(
        f"[#{loop_n:05d}]  "
        f"Wind={wind_kn:5.2f}kn  "
        f"Wave={wave_cm:5.1f}cm  "
        f"SOG(now)={sog_kn:6.2f}kn  "
        f"Vp({horizon_note})={vp_kn:6.2f}kn  "
        f"GPS={fix_str}({gps_sats}sat)  "
        f"APE={pct:5.1f}%  AvgAPE={avg_ape:5.1f}%{degraded_tag}"
    )


class SessionHistory:
    """One point per LOOP TICK (LOOP_HZ), pushed right after each
    LiveCSV row, so the PNG plots exactly the same data as the CSV.

    maxlen 500_000 ≈ 13.9 h at 10 Hz. Beyond that the oldest points
    are dropped and the plot would no longer start at session start.
    """
    def __init__(self, maxlen=500_000):
        # _ts holds wall-clock datetimes so the plot's time axis
        # can be formatted as hh:mm.
        self._ts, self._sog, self._vp, self._wind, self._wave = (
            collections.deque(maxlen=maxlen) for _ in range(5))

    def push(self, sog_kn, vp_kn, wind_kn, wave_hs_m):
        self._ts.append(datetime.now())
        self._sog.append(sog_kn); self._vp.append(vp_kn)
        self._wind.append(wind_kn); self._wave.append(wave_hs_m)

    def save_png(self, path="dashboard_final_plot2.png"):
        if len(self._ts) < 2:
            log.warning("  SessionHistory: not enough data to plot.")
            return
        ts = list(self._ts)
        sog = np.array(self._sog); vp = np.array(self._vp)
        wind = np.array(self._wind); wave = np.array(self._wave) * 100.0
        err = vp - sog

        # ── x-axis: exactly session start → session end ──────────
        t_start, t_end = ts[0], ts[-1]
        if t_end <= t_start:
            t_end = t_start + timedelta(seconds=1)
        tick_nums = np.linspace(mdates.date2num(t_start),
                                mdates.date2num(t_end), 6)
        time_fmt = mdates.DateFormatter("%H:%M")

        def style_time_axis(ax):
            ax.set_xlim(t_start, t_end)
            ax.set_xticks(tick_nums)
            ax.xaxis.set_major_formatter(time_fmt)
            ax.set_xlabel("Time (hh:mm)", fontsize=9)
            ax.grid(alpha=0.3)

        # ── y-axis: start at 0, top = 10% above the max ─────────
        def zero_based(ax, *arrays):
            top = max(float(np.max(a)) for a in arrays)
            ax.set_ylim(0, top * 1.1 if top > 0 else 1.0)

        fig = plt.figure(figsize=(15, 11))
        gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.6, wspace=0.38)

        ax1 = fig.add_subplot(gs[0, :])
        ax1.plot(ts, sog, color="#222", lw=1.2, label="SOG (kn)")
        ax1.plot(ts, vp, color="#57a773", lw=1.2, ls="--", label="Vp forecast (kn)")
        ax1.set_title("SOG vs Vp forecast", fontsize=9)
        ax1.set_ylabel("Speed (kn)", fontsize=9)
        style_time_axis(ax1)
        zero_based(ax1, sog, vp)
        ax1.legend(fontsize=8)

        ax2 = fig.add_subplot(gs[1, 0])
        ax2.plot(ts, wind, color="#5b8dd9")
        ax2.set_title("Wind (kn)", fontsize=9)
        ax2.set_ylabel("Wind speed (kn)", fontsize=9)
        style_time_axis(ax2)
        zero_based(ax2, wind)

        ax3 = fig.add_subplot(gs[1, 1])
        ax3.plot(ts, wave, color="#9b59b6")
        ax3.set_title("Wave Hs (cm)", fontsize=9)
        ax3.set_ylabel("Wave height Hs (cm)", fontsize=9)
        style_time_axis(ax3)
        zero_based(ax3, wave)

        # Error is signed (Vp − SOG), so it's centred on 0 rather than
        # starting at 0 — otherwise negative errors would be cut off.
        ax4 = fig.add_subplot(gs[2, 0])
        ax4.plot(ts, err, color="#e07b54")
        ax4.axhline(0, color="k", lw=0.8, ls="--")
        ax4.set_title("Error (kn)", fontsize=9)
        ax4.set_ylabel("Vp − SOG (kn)", fontsize=9)
        style_time_axis(ax4)
        lim = max(float(np.max(np.abs(err))) * 1.1, 0.5)
        ax4.set_ylim(-lim, lim)

        ax5 = fig.add_subplot(gs[2, 1])
        ax5.hist(np.abs(err) / np.maximum(np.abs(sog), 0.5) * 100,
                 bins=40, color="#e07b54")
        ax5.set_title("APE distribution", fontsize=9)
        ax5.set_xlabel("Absolute percentage error, APE (%)", fontsize=9)
        ax5.set_ylabel("Count (samples)", fontsize=9)
        ax5.set_xlim(left=0)
        ax5.set_ylim(bottom=0)
        ax5.grid(alpha=0.3)

        plt.savefig(path, dpi=130, bbox_inches="tight")
        log.info(f"  Session plot saved → {path}  ({len(ts)} points)")
        plt.close(fig)


# ══════════════════════════════════════════════════════════════
#  DASHBOARD PUSHER
# ══════════════════════════════════════════════════════════════
class DashboardPusher:
    def __init__(self, url):
        self.url = url
        self._q = queue.Queue(maxsize=20)
        self._sent = 0
        self._failed = 0
        threading.Thread(target=self._worker, daemon=True).start()

    def send(self, payload: dict):
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            pass

    def _worker(self):
        while True:
            payload = self._q.get()
            try:
                r = requests.post(self.url, json=payload, timeout=2)
                if r.status_code != 200:
                    log.debug(f"  Dashboard push → HTTP {r.status_code}")
                self._sent += 1
            except Exception as e:
                self._failed += 1
                log.debug(f"  Dashboard push error: {e}")
            self._q.task_done()


class LiveCSV:
    """One row per LOOP TICK (every 1/LOOP_HZ seconds).

    v10.10 columns (15): timestamp, wind_kn, wave_hs_m, roll_deg,
    pitch_deg, yaw_deg, sog_kn, sog_kmh, vp_forecast_kn,
    vp_forecast_kmh, alert_status, gps_fix, gps_sats, lat, lon.

    `alert_status` is the text label RED / YELLOW / GREEN.
    Wave height is stored in METRES, 4 decimal places.
    lat / lon are decimal degrees (6 dp); empty until the first
    valid GPGGA fix.

    Keeps the file handle open for the life of the run and flushes
    every FLUSH_EVERY rows instead of open/close per append() call.
    Call close() on shutdown.
    """
    HEADER = ["timestamp", "wind_kn", "wave_hs_m", "roll_deg", "pitch_deg", "yaw_deg",
              "sog_kn", "sog_kmh", "vp_forecast_kn", "vp_forecast_kmh",
              "alert_status", "gps_fix", "gps_sats", "lat", "lon"]
    FLUSH_EVERY = 20

    def __init__(self, path, maxrows=LOG_MAXROWS):
        self.path = path
        self.maxrows = maxrows
        self._count = 0
        self._fh = open(path, "w", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(self.HEADER)
        self._fh.flush()
        log.info(f"Live telemetry CSV log ready: {path} (~{LOOP_HZ}Hz)")

    def append(self, wind_kn, wave_hs_m, roll_deg, pitch_deg, yaw_deg,
               sog_kn, vp_kn, status, gps_fix, gps_sats, lat=None, lon=None):
        now = datetime.now()
        sog_kmh = sog_kn * KN_TO_KMH; vp_kmh = vp_kn * KN_TO_KMH
        row = [
            now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            round(wind_kn, 2), round(wave_hs_m, 4),
            round(roll_deg, 2), round(pitch_deg, 2), round(yaw_deg, 2),
            round(sog_kn, 3), round(sog_kmh, 3),
            round(vp_kn, 3), round(vp_kmh, 3),
            status, gps_fix, gps_sats,
            round(lat, 6) if lat is not None else "",
            round(lon, 6) if lon is not None else "",
        ]
        self._writer.writerow(row)
        self._count += 1
        if self._count % self.FLUSH_EVERY == 0:
            self._fh.flush()

    def close(self):
        try:
            self._fh.flush()
            self._fh.close()
        except Exception as e:
            log.debug(f"  LiveCSV close error: {e}")


def decide(vp):
    if vp >= THRESH_GREEN: return "GREEN"
    elif vp >= THRESH_YELLOW: return "YELLOW"
    else: return "RED"


# ══════════════════════════════════════════════════════════════
#  BRIDGE STATE RESET
# ══════════════════════════════════════════════════════════════
def reset_bridge_state(lags, aggregator, wave_e, imu_reader, ape_history):
    lags._buf = collections.deque([0.0] * lags._buf.maxlen, maxlen=lags._buf.maxlen)
    aggregator._wind_samples.clear()
    aggregator._sog_samples.clear()
    aggregator._wave_samples.clear()
    aggregator._window_start = time.time()
    wave_e.__init__()
    imu_reader._read_count = 0
    ape_history.clear()
    log.info("  Bridge sensor/inference state reset — MQTT connection untouched")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
def main():
    scaler, pinn, xgb_model, phys_coef, res_mean, res_std, alpha, horizon_s, resample_s = load_models()

    if resample_s <= 0:
        log.warning("  resample_seconds is 0/missing in meta.pkl — falling back to "
                     "1s aggregation windows. Predictions may not match training "
                     "conditions; re-check MODEL_DIR.")
        resample_s = 1

    pusher       = DashboardPusher(DASHBOARD_URL)
    hotkey       = HotkeyWatcher(key="r") if ENABLE_SOFT_RESET_HOTKEY else None
    lags         = LagBuffer(maxlen=10)
    gps_sog_est     = GpsSogEstimator()
    gps_fallback_sog = 0.0
    last_rmc_vtg_t   = 0.0
    aggregator   = ResampleAggregator(window_seconds=float(resample_s))
    live_csv     = LiveCSV(LIVE_LOG_PATH)
    wave_e       = WaveEstimator()
    imu_reader   = ImuReader()
    history      = SessionHistory()
    ape_history: list = []

    # inference-failure tracking (v10.7)
    consecutive_failures = 0
    last_forecast_source = "ml"   # "ml" | "physics_fallback" | "stale"

    # stationary-reset tracking (ZUPT-style)
    stationary_since = None

    log.info(f"Connecting to MQTT broker {MQTT_HOST}:{MQTT_PORT} (cloud relay, no LAN required)")
    ser = MqttReader(MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS,
                      TOPIC_DATA, TOPIC_STATUS, TOPIC_RESULTS, TOPIC_RESULTS_HEADER)

    t0 = time.time()
    while time.time() - t0 < 10.0:
        if ser.connected.is_set():
            break
        time.sleep(0.2)
    if not ser.connected.is_set():
        log.warning("  MQTT not connected yet after 10s — still retrying in the background; "
                    "calibration below will wait for data.")
    ser.publish_header(",".join(LiveCSV.HEADER))
    imu_reader.calibrate(ser)
    wind_baseline = calibrate_wind(ser)

    log.info("═" * 72)
    log.info("  Entering main loop — Ctrl-C to stop")
    log.info(f"  Live telemetry / dashboard / LiveCSV / plot rate : {LOOP_HZ} Hz")
    log.info(f"  Forecasting cadence                       : one aggregated sample every "
             f"{resample_s}s (matches training)")
    log.info(f"  Forecast horizon                          : Vp predicts SOG {horizon_s}s "
             f"AFTER each aggregated sample")
    log.info(f"  Dashboard: {DASHBOARD_URL}")
    log.info(f"  Publishing inference results back to: {TOPIC_RESULTS} (ESP32 logs these to SD)")
    log.info(f"  Stationary reset                          : SOG<{STATIONARY_SOG_THRESH_KN}kn for "
             f">{STATIONARY_RESET_S}s zeroes wave integrator")
    log.info(f"  Inference-failure recovery                 : falls back to physics-only "
             f"prediction after any exception, logs CRITICAL after "
             f"{MAX_CONSECUTIVE_FAILURES} consecutive failures")
    log.info(f"  CSV logging                               : LiveCSV only ({LIVE_LOG_PATH}) — "
             f"{len(LiveCSV.HEADER)} columns, alert_status = RED/YELLOW/GREEN, lat/lon included")
    log.info(f"  Session plot                              : every loop tick ({LOOP_HZ} Hz), "
             f"same rows as the CSV")
    if ENABLE_SOFT_RESET_HOTKEY:
        log.info(f"  Soft-reset hotkey: press 'r' in this console to recalibrate sensors")
    log.info("═" * 72)

    last_wind = last_sog = last_hs = 0.0
    last_vp = 0.0
    gps_fix = gps_sats = 0
    last_lat = last_lon = None     # decimal degrees; None until first valid GPGGA fix
    target_dt = 1.0 / LOOP_HZ
    print_dt = 1.0 / CONSOLE_PRINT_HZ
    last_output_t = 0.0
    last_print_t = 0.0
    loop_count = 0
    forecast_count = 0
    line_counts = collections.Counter()

    KNOWN_IGNORE = {"$YXXDR", "$HCHDG", "$HCHDT", "$GPZDA", "$WIMDA",
                    "$GPGSV", "$GPGLL", "$GPGSA", "$HCHDM", "$HCHIM",
                    "$PGRME", "$PGRMZ", "$GPRTE"}

    try:
        while True:
            # ── soft-reset hotkey check — no-op entirely when disabled ──
            if hotkey is not None and hotkey.triggered.is_set():
                hotkey.triggered.clear()
                log.info("  [HOTKEY] Soft reset triggered — sensors only, WiFi/MQTT untouched")
                ser.publish_command("SOFT_RESET")
                reset_bridge_state(lags, aggregator, wave_e, imu_reader, ape_history)
                stationary_since = None   # don't carry stale timer across a reset
                consecutive_failures = 0  # a soft reset is also a chance to
                                           # recover from a stuck inference state

            try:
                line = ser.readline().decode(errors="ignore").strip()
            except Exception as e:
                log.warning(f"MQTT read error: {e}")
                continue
            if not line or is_boot_noise(line):
                continue

            prefix = line[:6] if line.startswith("$") else line.split(":")[0]
            line_counts[prefix] += 1

            if line.startswith("IMU:"):
                az = imu_reader.read(line)
                if az is not None:
                    last_hs = wave_e.update(az, time.time())
            elif line.startswith("$WIMWV"):
                spd = parse_wimwv(line)
                if spd is not None:
                    last_wind = max(0.0, spd - wind_baseline)
            elif line.startswith(("$GPGGA", "$GNGGA")):
                fix, sats = parse_gpgga(line)
                gps_fix, gps_sats = fix, sats
                latlon = parse_gpgga_latlon(line)
                if latlon is not None:
                    last_lat, last_lon = latlon
                    gps_fallback_sog = gps_sog_est.update(latlon[0], latlon[1], time.time())
            elif line.startswith(("$GPRMC", "$GNRMC", "$GPVTG", "$GNVTG", "$IIVTG")):
                sog = parse_sog(line)
                if sog is not None:
                    last_sog = sog
                    last_rmc_vtg_t = time.time()
            elif MOCK_SOG is not None:
                  last_sog = float(MOCK_SOG)

            # ── stationary reset (ZUPT-style) ─────────────────
            if last_sog < STATIONARY_SOG_THRESH_KN:
                if stationary_since is None:
                    stationary_since = time.time()
                elif time.time() - stationary_since > STATIONARY_RESET_S:
                    wave_e.stationary_reset()
                    last_hs = wave_e.hs
                    stationary_since = time.time()   # avoid resetting every single tick
            else:
                stationary_since = None

            if time.time() - last_rmc_vtg_t > RMC_VTG_STALE_S:
                last_sog = gps_fallback_sog
            aggregator.push(last_wind, last_sog, last_hs)

            now = time.perf_counter()
            if now - last_output_t < target_dt:
                continue
            last_output_t = now
            loop_count += 1

            is_new_forecast = False

            if aggregator.ready():
                agg = aggregator.flush()
                if agg is not None:
                    agg_wind, agg_sog, agg_wave = agg
                    lags.push(agg_sog)
                    forecast_count += 1
                    is_new_forecast = True

                    # ── inference with real recovery (v10.7) ──
                    # On failure: fall back to the cheap physics-only
                    # prediction (still responds to current inputs)
                    # instead of freezing at the last successful ML
                    # output forever. Track consecutive failures and
                    # escalate to log.critical() so this is impossible
                    # to miss live, not just in a post-hoc CSV review.
                    try:
                        vp = infer(agg_wind, agg_wave, agg_sog, lags, scaler, pinn,
                                   xgb_model, phys_coef, res_mean, res_std, alpha)
                        last_vp = vp
                        last_forecast_source = "ml"
                        consecutive_failures = 0
                    except Exception as e:
                        consecutive_failures += 1
                        log.error(f"Inference error ({consecutive_failures} consecutive): {e}",
                                  exc_info=True)
                        try:
                            true_wind_kn = max(0.0, agg_wind - agg_sog)
                            vp = physics_predict(true_wind_kn, agg_wave, lags.lag1, phys_coef)
                            last_vp = vp
                            last_forecast_source = "physics_fallback"
                        except Exception as e2:
                            # physics_predict is a simple closed-form calc — if THIS
                            # throws too, something is fundamentally wrong (e.g. coef
                            # array itself corrupted), not just a one-off bad sample.
                            log.error(f"Physics fallback ALSO failed: {e2}", exc_info=True)
                            vp = last_vp
                            last_forecast_source = "stale"
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            log.critical(
                                f"Inference has failed {consecutive_failures} consecutive "
                                f"times — forecast is running DEGRADED "
                                f"(source={last_forecast_source}). Check the traceback "
                                f"above for the root cause; consider a soft-reset "
                                f"('r' hotkey) if lag-buffer state looks suspect."
                            )

                    pct = compute_pct_error(agg_sog, vp)
                    ape_history.append(pct)
                    avg_ape = float(np.mean(ape_history))

                    # NOTE (v10.11): history.push() is no longer called here.
                    # It runs every loop tick below, alongside live_csv.append().
                    status = decide(vp)

                    degraded_note = "" if last_forecast_source == "ml" else f"  [{last_forecast_source}]"
                    log.info(f"[forecast #{forecast_count:05d}]  "
                             f"SOG(now)={agg_sog:.2f}kn  →  "
                             f"Vp(+{horizon_s}s)={vp:.2f}kn  "
                             f"AvgAPE={avg_ape:.1f}%{degraded_note}")

            # ── LIVE TRACK — runs EVERY loop tick (LOOP_HZ) ──────────
            live_status = decide(last_vp)
            live_payload = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "vp_forecast_kn": round(last_vp, 3),
                "vp_forecast_kmh": round(last_vp * KN_TO_KMH, 3),
                "forecast_horizon_s": horizon_s,
                "is_new_forecast": is_new_forecast,
                "forecast_source": last_forecast_source,
                "sog_now_kn": round(last_sog, 3),
                "sog_kmh_now": round(last_sog * KN_TO_KMH, 3),
                "wind_kn": round(last_wind, 2),
                "wave_hs_m": round(last_hs, 4),
                "wave_cm": round(last_hs * 100.0, 2),
                "wave_hs_cm_3dp": round(last_hs * 100.0, 2),
                "roll": round(imu_reader.last_roll, 2),
                "pitch": round(imu_reader.last_pitch, 2),
                "yaw": round(imu_reader.last_yaw, 2),
                "heave": round(wave_e.disp, 4),
                "gps_fix": gps_fix,
                "gps_sats": gps_sats,
                "lat": round(last_lat, 6) if last_lat is not None else None,
                "lon": round(last_lon, 6) if last_lon is not None else None,
                "status": live_status,
            }
            pusher.send(live_payload)

            # SD-mirror row — column order MUST match LiveCSV.HEADER (15 cols).
            # None (no fix yet) becomes an empty cell, not the string "None".
            sd_row = ",".join("" if v is None else str(v) for v in [
                live_payload["ts"], live_payload["wind_kn"], live_payload["wave_hs_m"],
                live_payload["roll"], live_payload["pitch"], live_payload["yaw"],
                live_payload["sog_now_kn"], live_payload["sog_kmh_now"],
                live_payload["vp_forecast_kn"], live_payload["vp_forecast_kmh"],
                live_payload["status"], live_payload["gps_fix"], live_payload["gps_sats"],
                live_payload["lat"], live_payload["lon"],
            ])
            ser.publish_result(sd_row)

            live_csv.append(
                wind_kn=last_wind, wave_hs_m=last_hs,
                roll_deg=imu_reader.last_roll, pitch_deg=imu_reader.last_pitch,
                yaw_deg=imu_reader.last_yaw,
                sog_kn=last_sog, vp_kn=last_vp,
                status=live_status,
                gps_fix=gps_fix, gps_sats=gps_sats,
                lat=last_lat, lon=last_lon,
            )

            # v10.11: session-plot history at the SAME rate as the CSV
            # (one point per loop tick, not one per forecast).
            history.push(last_sog, last_vp, last_wind, last_hs)

            if now - last_print_t >= print_dt:
                last_print_t = now
                pct_display = compute_pct_error(last_sog, last_vp) if forecast_count else 0.0
                print_status_line(last_wind, last_hs * 100.0, last_sog, last_vp, horizon_s,
                                   gps_fix, gps_sats, loop_count, pct_display,
                                   float(np.mean(ape_history)) if ape_history else 0.0,
                                   forecast_source=last_forecast_source)

    except KeyboardInterrupt:
        log.info("Shutdown ...")
    finally:
        ser.close()
        live_csv.close()
        log.info(f"Total loop ticks : {loop_count}")
        log.info(f"Total forecasts made : {forecast_count}")
        log.info(f"Line type summary: {dict(line_counts.most_common())}")
        if ape_history:
            arr = np.array(ape_history)
            log.info(f"  Mean APE={arr.mean():.2f}%  Median APE={np.median(arr):.2f}%")
        history.save_png("dashboard_plot.png")
        log.info("Done.")


if __name__ == "__main__":
    main()