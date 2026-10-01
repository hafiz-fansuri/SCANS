"""
Vessel Speed / Wind / Wave Analysis Pipeline (CSV-only)
==========================================================
Inputs:
  1. vessel_live_log_SIM_merged.csv  -- dense onboard prototype/inference log
     (SOG, wind, wave, forecast, lat/lon, attitude; ~5-10 Hz, one row per inference tick)
  2. Jati_6_*.xlsx                   -- 1-min ground-truth vessel telemetry

Pipeline:
  1. Load + clean the prototype CSV
  2. Load Jati 6 ground truth
  3. Match timestamps (prototype floored+averaged to Jati 6's 1-min grid)
  4. Save merged dataset as CSV (no plots)

Output (in OUT_DIR):
  - vessel_jati6_merged.csv   (lat, lon, roll_deg, pitch_deg, yaw_deg included)

Assumptions -- edit CONFIG below if any of these are wrong for your dataset:
  - Both timestamps are the same wall clock (both effectively UTC+8). Confirmed
    on this dataset: 100% of Jati6 minutes had a matching prototype minute, no offset needed.
    If you swap in a different pair of files, re-check this (see coverage % printed).
  - CSV: sog_kn = SOG (kn), wind_kn = wind speed (kn), wave_hs_m = sig. wave height (m),
    vp_forecast_kn = model's forecast speed (kn), gps_fix = fix quality (0 = no fix),
    lat/lon = decimal degrees, roll_deg/pitch_deg/yaw_deg = attitude (deg).
    yaw_deg is assumed 0-360 (or -180..180, doesn't matter -- circular mean handles both).
  - Jati6: VESSEL_KNOTS = ground-truth SOG (kn), WIND_SPEED_KNOTS (kn), SIG_WAVE_HEIGHT_M (m).
  - Cleaning rules applied to the prototype CSV only (Jati6 is treated as ground truth, not cleaned):
      * drop rows with unparseable timestamp
      * drop rows with gps_fix < MIN_GPS_FIX (no GPS fix -> SOG/position meaningless)
      * drop rows with sog_kn outside [0, MAX_PLAUSIBLE_SOG_KN] (sensor glitches)
      * drop exact duplicate timestamps
      * drop rows still missing sog_kn/wind_kn/wave_hs_m after the above
    (lat/lon/roll/pitch/yaw are NOT part of the drop criteria -- a row can be missing
    attitude data and still be kept if SOG/wind/wave are valid.)
  - Per-minute aggregation: lat, lon, roll_deg, pitch_deg use a plain mean (fine --
    they don't wrap). yaw_deg uses a circular mean (atan2 of sin/cos), because a plain
    mean is wrong across the 0/360 boundary.
"""

import os
import numpy as np
import pandas as pd

# ---------------- CONFIG (edit for your files) ----------------
CSV_PATH = r"C:\Users\fansuri\Downloads\vessel_replay_forecast_OUT.csv"
XLSX_PATH = r"E:\Jati_6_46985_081026121800-081026141859.xlsx"
OUT_DIR = r"C:\Users\fansuri\Documents\sea trial_final"

MAX_PLAUSIBLE_SOG_KN = 40.0   # anything above this on this vessel is a glitch, not real
MIN_GPS_FIX = 1               # gps_fix: 0 = no fix -> drop; 1/2 = valid fix -> keep

# column names for the prototype CSV -- edit here if your CSV uses different headers
LAT_COL = "lat"
LON_COL = "lon"

os.makedirs(OUT_DIR, exist_ok=True)


# ---------------- STEP 1: LOAD + CLEAN PROTOTYPE CSV ----------------
def load_and_clean_sim(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    n0 = len(df)

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])

    numeric_cols = ["sog_kn", "wind_kn", "wave_hs_m", "roll_deg", "pitch_deg",
                     "yaw_deg", "vp_forecast_kn", "gps_fix", "gps_sats",
                     LAT_COL, LON_COL]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            print(f"[warn] expected column '{c}' not found in CSV -- skipping it")

    if "gps_fix" in df.columns:
        df = df[df["gps_fix"] >= MIN_GPS_FIX]

    df = df[df["sog_kn"].between(0, MAX_PLAUSIBLE_SOG_KN)]

    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp")
    df = df.dropna(subset=["sog_kn", "wind_kn", "wave_hs_m"])  # core fields only -- see docstring

    print(f"[clean] prototype CSV: {n0} -> {len(df)} rows "
          f"({n0 - len(df)} dropped: bad timestamp / no GPS fix / SOG out of range / dupes / NaN)")
    return df.reset_index(drop=True)


# ---------------- STEP 2: LOAD JATI 6 GROUND TRUTH ----------------
def load_jati6(xlsx_path: str) -> pd.DataFrame:
    df = pd.read_excel(xlsx_path)
    df = df.rename(columns={df.columns[0]: "timestamp_raw"})
    df["minute"] = pd.to_datetime(df["timestamp_raw"], format="%m/%d/%Y %H:%M")

    keep = ["minute", "VESSEL_KNOTS", "WIND_SPEED_KNOTS", "SIG_WAVE_HEIGHT_M",
            "HEADING", "OPERATING_MODE"]
    print(f"[load] Jati 6: {len(df)} rows, {df['minute'].min()} -> {df['minute'].max()}")
    return df[keep].sort_values("minute").reset_index(drop=True)


# ---------------- STEP 3: MATCH TIMESTAMPS ----------------
def _circular_mean_deg(angles_deg: pd.Series) -> float:
    """Mean of angles in degrees, correct across the 0/360 wrap. NaNs ignored."""
    a = np.deg2rad(angles_deg.dropna())
    if len(a) == 0:
        return np.nan
    mean_angle = np.arctan2(np.sin(a).mean(), np.cos(a).mean())
    return np.rad2deg(mean_angle) % 360


def match_timestamps(sim: pd.DataFrame, jati: pd.DataFrame) -> pd.DataFrame:
    sim = sim.copy()
    sim["minute"] = sim["timestamp"].dt.floor("min")

    agg_dict = {
        "sog_kn": ("sog_kn", "mean"),
        "wind_kn": ("wind_kn", "mean"),
        "wave_hs_m": ("wave_hs_m", "mean"),
        "vp_forecast_kn": ("vp_forecast_kn", "mean"),
        "n_samples": ("sog_kn", "count"),
    }
    if LAT_COL in sim.columns:
        agg_dict["lat"] = (LAT_COL, "mean")
    if LON_COL in sim.columns:
        agg_dict["lon"] = (LON_COL, "mean")
    if "roll_deg" in sim.columns:
        agg_dict["roll_deg"] = ("roll_deg", "mean")
    if "pitch_deg" in sim.columns:
        agg_dict["pitch_deg"] = ("pitch_deg", "mean")

    sim_agg = sim.groupby("minute").agg(**agg_dict).reset_index()

    # yaw_deg handled separately -- needs circular mean, not plain .agg("mean")
    if "yaw_deg" in sim.columns:
        yaw_agg = (sim.groupby("minute")["yaw_deg"]
                      .apply(_circular_mean_deg)
                      .reset_index(name="yaw_deg"))
        sim_agg = pd.merge(sim_agg, yaw_agg, on="minute", how="left")

    merged = pd.merge(jati, sim_agg, on="minute", how="inner")
    merged = merged.sort_values("minute").reset_index(drop=True)

    coverage = len(merged) / len(jati) * 100 if len(jati) else 0
    print(f"[match] Jati6 minutes: {len(jati)} | prototype minutes: {len(sim_agg)} | "
          f"Matched: {len(merged)} ({coverage:.1f}% Jati6 coverage)")
    if coverage < 100:
        missing = jati.loc[~jati["minute"].isin(sim_agg["minute"]), "minute"]
        print(f"[match] WARNING: {len(missing)} Jati6 minutes had no prototype data "
              f"({missing.min()} .. {missing.max()})")
    return merged


# ---------------- MAIN ----------------
if __name__ == "__main__":
    sim = load_and_clean_sim(CSV_PATH)
    jati = load_jati6(XLSX_PATH)
    merged = match_timestamps(sim, jati)

    merged_csv_path = os.path.join(OUT_DIR, "vessel_jati6_merged2.csv")
    merged.to_csv(merged_csv_path, index=False)
    print(f"[save] merged dataset -> {merged_csv_path}")
    print("[done]")