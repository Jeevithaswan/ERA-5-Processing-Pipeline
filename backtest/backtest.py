"""
Event-by-event back-test of the Pandhurna citrus warning thresholds on ERA5-Land.

For one reported incident (sheet 1 of the back-test workbook) this:
  1. downloads only the months that event needs, for one small box that
     covers every event cell (shared by all events, so nothing is fetched twice);
  2. applies each threshold listed for the event (definitions: sheet 2) at the
     event's grid cell, from 15 days before the incident start to its end;
  3. writes a per-rule result, a daily CSV, a timeline chart, a risk map and a
     rules-and-sources note to backtest/events/<ID>/;
  4. fills columns S, T, U and X for that event in a copy of the workbook
     (backtest/Pandhurna-Backtest-RESULTS.xlsx). The original is never touched.

HOW TO RUN (from the project folder)
------------------------------------
    python backtest/backtest.py list
    python backtest/backtest.py download I01     # fetch the data only
    python backtest/backtest.py run I01          # download if needed, evaluate, write outputs

Rules are implemented one event at a time. An ID that is not implemented yet
is reported as such, never silently skipped.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch

BT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
import pipeline as pl  # noqa: E402

WORKBOOK = PROJECT_ROOT / "280926-Pandhurna-Backtest- V2.xlsx"
RESULTS_XLSX = BT_DIR / "Pandhurna-Backtest-RESULTS.xlsx"
RAW_DIR = BT_DIR / "era5_raw"
EVENTS_DIR = BT_DIR / "events"
LOG_DIR = BT_DIR / "logs"

DATASET = "reanalysis-era5-land"
# One box covering every event cell in sheet 1 (21.1-21.6 N, 78.4-79.1 E) plus a margin.
AREA = [21.8, 78.3, 20.9, 79.3]  # North, West, South, East
LEAD_DAYS = 15
IST = pd.Timedelta(hours=5, minutes=30)

# Spatial multi-year mode (manager's extension request): a separate box, wider than
# AREA -- extended to fully cover Pandhurna's exact boundary (from Pandhurna.kml)
# UNION Chhindwara district's exact boundary (from Chhindwara.kml, 22.817/78.245/
# 21.462/79.407), plus a small margin -- a gap the single-point per-event box
# always had, since it was sized to event locations, not either boundary's outline.
# A separate raw-data folder, so this never silently mixes with or re-downloads
# over the narrower per-event boxes the single-point back-test results above
# already depend on.
WIDE_AREA = [22.9, 78.2, 21.4, 79.5]  # North, West, South, East
SPATIAL_RAW_DIR = BT_DIR / "era5_raw_spatial"
SPATIAL_DIR = BT_DIR / "spatial"

CDS_NAMES = {
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "tp": "total_precipitation",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "ssrd": "surface_solar_radiation_downwards",
    "sp": "surface_pressure",
    "fg10": "10m_wind_gust_since_previous_post_processing",
}
# Wind gust is not in ERA5-Land; it comes from ERA5 single levels (0.25°) and is
# matched to the ERA5-Land grid by nearest neighbour. Its box is padded by one
# 0.25° step so every ERA5-Land cell has a gust point within 0.125°.
DATASETS = {"fg10": "reanalysis-era5-single-levels"}
GUST_AREA = [22.0, 78.0, 20.75, 79.5]

LEVEL_NAMES = ["None", "Low", "Moderate", "High"]
# Status palette (dataviz references/palette.md): alert levels are states, so
# they take the reserved status steps and always ship with a text label.
LEVEL_COLORS = ["#e1e0d9", "#fab219", "#ec835a", "#d03b3b"]
EVENT_RULE_COLOR = "#2a78d6"
CALENDAR_COLOR = "#898781"

# Pipeline settings reused so both tools agree on the definitions.
_DEFAULTS = pl.load_defaults()
LEAF_WET_RH = _DEFAULTS["leaf_wetness_rh_pct"]   # 90 %, Sentelhas et al. (2008)
RAIN_DAY_MM = _DEFAULTS["rain_day_mm"]            # 2.5 mm, IMD

# FAO-56 parameters for X-W01 (citrus on clay), from the tables the sheet cites.
KC_CITRUS = 0.70          # Table 12, citrus without ground cover: Kc ini/end 0.70, mid 0.65
ROOT_DEPTH_M = 1.35       # Table 22, citrus 1.2-1.5 m (mid value)
P_DEPLETION = 0.50        # Table 22, citrus
THETA_FC, THETA_WP = 0.36, 0.22   # Table 19, clay: FC 0.32-0.40, WP 0.20-0.24 (mid values)
TAW_MM = 1000 * (THETA_FC - THETA_WP) * ROOT_DEPTH_M


# =============================================================================
# Workbook
# =============================================================================

def _copy_workbook(dest: Path) -> None:
    # Excel's lock blocks Python's open(); the Windows shell copy still succeeds.
    try:
        shutil.copy(WORKBOOK, dest)
    except PermissionError:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Copy-Item -LiteralPath '{WORKBOOK}' -Destination '{dest}'"], check=True)


def read_workbook() -> tuple[pd.DataFrame, pd.DataFrame]:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "backtest.xlsx"
        _copy_workbook(copy)
        events = pd.read_excel(copy, sheet_name=0, header=2)
        thresholds = pd.read_excel(copy, sheet_name=1, header=2)
    events = events[events["Incident ID"].notna()].copy()
    events["cell"] = events["ERA5-Land 0.1° cell used by us (centre)"].astype(str).str.split().str[0]
    events["lat"] = events["cell"].str.split("_").str[0].astype(float)
    events["lon"] = events["cell"].str.split("_").str[1].astype(float)
    events["start"] = pd.to_datetime(events["Incident period - start"]).dt.date
    events["end"] = pd.to_datetime(events["Incident period - end"]).dt.date
    events["rules"] = events["Thresholds to test (IDs; see sheet 2)"].str.split(",").apply(
        lambda xs: [x.strip() for x in xs])
    thresholds = thresholds[thresholds["ID"].notna()].set_index("ID")
    return events.set_index("Incident ID"), thresholds


# =============================================================================
# Rules (sheet 2)
#   daily:    boolean per day -> 7-day index = passing days in the last 7,
#             High >= 5, Moderate >= 3, Low >= 1
#   event:    boolean per day, no levels (alert / no alert)
#   calendar: active months only, no weather data
# "impl" records how the sheet's wording was turned into code -- it is copied
# into each event's rules-and-sources note so every choice is visible.
# =============================================================================

def _in_months(d: xr.Dataset, months: list[int]) -> xr.DataArray:
    return d["time"].dt.month.isin(months)


def _dd_flag(d: xr.Dataset, key: str, per_gen: float, window: float) -> xr.DataArray:
    """True on days inside the last `window` DD of a generation: accumulated DD
    (reset 1 January) mod `per_gen` >= per_gen - window, or a day on which a
    generation boundary was crossed."""
    cum = d[key].groupby("time.year").cumsum("time")
    prev = cum - d[key]
    crossed = np.floor(cum / per_gen) > np.floor(prev / per_gen)
    return ((cum % per_gen) >= (per_gen - window)) | crossed


DAILY_RULES = {
    "H01": {"vars": ["t2m"], "tmax_line": 37,
            "impl": "Daily maximum of hourly 2 m temperature > 37 °C, only on days in March or April.",
            "fn": lambda d: (d["tmax"] > 37) & _in_months(d, [3, 4])},
    "H02": {"vars": ["t2m"], "tmax_line": 30,
            "impl": "Daily maximum of hourly 2 m temperature > 30 °C.",
            "fn": lambda d: d["tmax"] > 30},
    "H03": {"vars": ["t2m"], "tmax_line": 33,
            "impl": "Daily maximum of hourly 2 m temperature > 33 °C.",
            "fn": lambda d: d["tmax"] > 33},
    "H06": {"vars": ["t2m"], "tmax_line": 35,
            "impl": "Daily maximum of hourly 2 m temperature > 35 °C, only in September or October.",
            "fn": lambda d: (d["tmax"] > 35) & _in_months(d, [9, 10])},
    "P01": {"vars": ["t2m"],
            "impl": "Daily mean of hourly 2 m temperature between 25 and 30 °C inclusive.",
            "fn": lambda d: (d["tmean"] >= 25) & (d["tmean"] <= 30)},
    "G-D02": {"vars": ["t2m", "d2m"],
              "impl": f"Leaf-wetness hours = hours with RH >= {LEAF_WET_RH} % (RH from 2 m temperature and "
                      "dewpoint, Magnus form as in pipeline.py); day passes if >= 18 wet hours and daily mean "
                      "temperature > 22 °C, only July-October.",
              "fn": lambda d: (d["lw_hours"] >= 18) & (d["tmean"] > 22) & _in_months(d, [7, 8, 9, 10])},
    "G-D01": {"vars": ["t2m", "d2m", "tp"],
              "impl": f"Day passes if (daily rain >= {RAIN_DAY_MM} mm, IMD rain day, or leaf-wetness hours > 10, "
                      f"RH >= {LEAF_WET_RH} % proxy) and daily mean temperature is 20-28 °C inclusive.",
              "fn": lambda d: ((d["rain_mm"] >= RAIN_DAY_MM) | (d["lw_hours"] > 10))
                              & (d["tmean"] >= 20) & (d["tmean"] <= 28)},
    "G-P01": {"vars": ["t2m"], "from_jan1": True,
              "impl": "Daily degree-days = mean over the 24 hours of (hourly temperature capped to 10.5-33.0 °C "
                      "minus 10.5) -- horizontal cut-off. Accumulated from 1 January; flag when the running total "
                      "is in the last 63.3 DD of a 250 DD generation.",
              "fn": lambda d: _dd_flag(d, "dd_gp01", 250.0, 63.3)},
    "G-P03": {"vars": ["t2m"], "from_jan1": True,
              "impl": "As G-P01 with 8.5 °C base, 27.5 °C cut-off, 666.67 DD per generation, flag = last 182.3 DD.",
              "fn": lambda d: _dd_flag(d, "dd_gp03", 666.67, 182.3)},
    "G-P02": {"vars": ["t2m"], "from_jan1": True,
              "impl": "As G-P01 with 10.8 °C base, 32.0 °C cut-off, 237.4 DD per generation, flag = last 116.5 DD.",
              "fn": lambda d: _dd_flag(d, "dd_gp02", 237.4, 116.5)},
    "G-P05": {"vars": ["t2m"], "from_jan1": True,
              "impl": "As G-P01 with 10.6 °C base, 33.0 °C cut-off, 437.4 DD per generation, flag = last 79.2 DD.",
              "fn": lambda d: _dd_flag(d, "dd_gp05", 437.4, 79.2)},
    "P02": {"vars": ["t2m", "d2m"],
            "impl": "Daily minimum temperature > 18 °C and daily mean actual vapour pressure (from hourly dewpoint, "
                    "Magnus form) > 20 mm Hg = 26.66 hPa. The sheet's third condition (sunshine < 6 h) cannot be "
                    "computed from ERA5-Land and is omitted, as sheet 2 notes.",
            "fn": lambda d: (d["tmin"] > 18) & (d["vp_hpa"] > 20 * 1.33322)},
    "D01": {"vars": ["t2m", "d2m", "u10", "v10"],
            "impl": "Same day: Tmin >= 24 °C, Tmax <= 37 °C, daily mean RH >= 76 % and daily mean 10 m wind speed "
                    ">= 6.4 km/h (1.78 m/s). Directions (>= / <=) as sheet 2 notes.",
            "fn": lambda d: (d["tmin"] >= 24) & (d["tmax"] <= 37) & (d["rh_mean"] >= 76) & (d["u10"] * 3.6 >= 6.4)},
    "D02": {"vars": ["t2m", "tp", "u10", "v10"],
            "impl": "Day passes if at least one hour has rain >= 0.1 mm (IMD trace limit) and 10 m wind speed "
                    "> 8 m/s in the same hour. CAVEAT: ERA5-Land hourly 10 m wind is a ~9 km area mean and never "
                    "exceeded 8 m/s anywhere in the back-test box in Jan 2020 - Jun 2021 (maximum 7.6 m/s), so "
                    "this rule effectively cannot fire on ERA5-Land. The source specifies average wind during "
                    "rain, so gusts are not substituted.",
            "fn": lambda d: d["wdr_hours"] >= 1},
}

# Event rules whose own condition sets Low / Moderate / High directly (no 7-day index).
LEVEL_RULES = {
    "H05": {"vars": ["t2m", "tp"], "from_date": (6, 8),
            "impl": f"Dry day = daily rain < {RAIN_DAY_MM} mm (IMD rain-day threshold, as sheet 2 notes). Consecutive "
                    "dry days are counted only inside the monsoon window 8 June - 7 October (the count restarts "
                    "each 8 June). Dry spell >= 15 days = Moderate, >= 20 days = High (the sheet gives '>= 15' as "
                    "the alert and '>= 20 = High'; Moderate for 15-19 days is our reading).",
            "fn": lambda d: _dry_spell_level(d)},
    "X-S01": {"vars": ["t2m", "tp", "fg10"],
              "impl": "Only outside the monsoon (8 Oct - 7 Jun). Daily peak gust = highest hourly ERA5 10 m wind "
                      "gust (0.25°, nearest point to the ERA5-Land cell) in km/h; rain from ERA5-Land. "
                      "High: gust >= 40 km/h and rain >= 2.5 mm; Moderate: gust >= 30 and rain >= 2.5 mm; "
                      "Low: gust >= 30 and rain >= 0.1 mm.",
              "fn": lambda d: _storm_level(d)},
}

EVENT_RULES = {
    "X-W01": {"vars": ["t2m", "d2m", "tp", "u10", "v10", "ssrd", "sp"], "from_jan1": True,
              "impl": (f"Daily FAO-56 root-zone water balance (Eq. 85, no irrigation, no runoff): depletion Dr "
                       f"starts at TAW on the first day of data (dry season) and each day Dr = Dr - rain + ETc, "
                       f"bounded to 0..TAW; Dr = 0 is field capacity (surplus drains). ETc = Ks x Kc x ET0 "
                       f"(Eq. 84 stress coefficient). ET0 = FAO-56 Penman-Monteith (Eq. 6) from daily Tmax/Tmin, "
                       f"mean dewpoint, 10 m wind converted to 2 m (Eq. 47), solar radiation and surface pressure. "
                       f"Kc = {KC_CITRUS} (Table 12), root depth {ROOT_DEPTH_M} m and p = {P_DEPLETION} (Table 22), "
                       f"clay FC {THETA_FC} / WP {THETA_WP} (Table 19) -> TAW = {TAW_MM:.0f} mm. Alert on every day "
                       f"that completes a run of >= 3 consecutive days with the root zone at field capacity and "
                       f"rain >= ETc."),
              "fn": lambda d: _run_at_least(d["wb_wet_day"], 3)},
}

# Calendar advisories: (active months, first active day "MM-DD" in the first month, or None).
CALENDAR_RULES = {
    "T02": ([2, 3], None), "T04": ([2, 3, 4, 8, 9, 10], None),
    "T09": ([8, 9, 10], "08-15"), "T10": ([9, 10, 11], None),
    "T11": ([8, 9, 10, 11], None), "T12": ([4, 5, 10, 11, 12], None),
    "T13": ([4, 8, 12], None), "A05": ([6, 7, 8, 9], None),
    "A06": ([4, 5], None), "X-B01": ([7, 8], "07-01"),
    "X-G01": ([5, 6], "05-15"), "X-G02": ([10], "10-08"),
}

# Need the 1991-2020 normal or the IMD Nagpur bias correction: anomaly stage.
DEFERRED_RULES = {"H04", "X-H04c", "X-H01c", "X-H01"}

# Plain-English name for each rule ID, used only in charts/output for readability --
# the ID itself (as sheet 2 uses it) is always kept alongside it, never replaced.
RULE_LABEL = {
    "H01": "Heat stress, Mar-Apr", "H02": "Heat stress (Tmax>30C)", "H03": "Heat stress (Tmax>33C)",
    "H06": "Sunscald heat, Sep-Oct", "H05": "Monsoon dry spell", "H04": "Heat wave (IMD)",
    "P01": "Psylla-favourable temp", "P02": "Leaf-miner-favourable temp",
    "G-D02": "Fungus risk (Phytophthora)", "G-D01": "Fungus risk (Alternaria)",
    "G-D03": "Fungus risk (Scab)",
    "G-P01": "Psylla new-adults due", "G-P02": "Leaf-miner new-adults due",
    "G-P03": "Mealybug new-adults due", "G-P05": "Fruit-fly new-adults due",
    "D01": "Canker-favourable weather", "D02": "Canker wind-driven rain",
    "X-W01": "Waterlogging risk", "X-S01": "Hail/storm risk",
    "X-H01": "Pre-monsoon heat/dry stress", "X-H01c": "Heat stress, Mar-Apr (corrected)",
    "X-H04c": "Heat wave, corrected (IMD)",
    "T02": "Psylla peak season", "T04": "Leaf-miner peak season", "T09": "Brown-rot season",
    "T10": "Fruit-fly season", "T11": "Fruit-sucking-moth season", "T12": "Mite season",
    "T13": "Blackfly season", "A05": "Monsoon waterlogging season", "A06": "Summer heat-stress season",
    "X-B01": "Early brown-rot watch", "X-G01": "Pre-monsoon trunk-paste window",
    "X-G02": "Post-monsoon trunk-paste window",
}


def rule_label(r: str) -> str:
    """'Plain name (ID)' for charts -- ID always kept so it still maps back to sheet 2."""
    name = RULE_LABEL.get(r)
    return f"{name} ({r})" if name else r


def _run_at_least(flag: xr.DataArray, n: int) -> xr.DataArray:
    """True on days that are at least the n-th consecutive True day."""
    return _run_length(flag) >= n


def _monsoon(d: xr.Dataset) -> xr.DataArray:
    """8 June - 7 October (ICAR-CRIDA Chhindwara plan: onset 2nd week June, withdrawal 1st week October)."""
    md = d["time"].dt.month * 100 + d["time"].dt.day
    return (md >= 608) & (md <= 1007)


def _run_length(flag: xr.DataArray) -> xr.DataArray:
    """Length of the current run of consecutive True days, per day."""
    def runs(x):
        out = np.zeros_like(x, dtype=int)
        c = 0
        for i, v in enumerate(x):
            c = c + 1 if v else 0
            out[i] = c
        return out
    run = xr.apply_ufunc(runs, flag.fillna(False).astype(bool), input_core_dims=[["time"]],
                         output_core_dims=[["time"]], vectorize=True)
    return run.transpose(*flag.dims)


def _dry_spell_level(d: xr.Dataset) -> xr.DataArray:
    run = _run_length((d["rain_mm"] < RAIN_DAY_MM) & _monsoon(d))
    return xr.where(run >= 20, 3, xr.where(run >= 15, 2, 0)).where(d["rain_mm"].notnull())


def _storm_level(d: xr.Dataset) -> xr.DataArray:
    g, r = d["gust_kmh"], d["rain_mm"]
    lvl = xr.where((g >= 40) & (r >= RAIN_DAY_MM), 3,
                   xr.where((g >= 30) & (r >= RAIN_DAY_MM), 2,
                            xr.where((g >= 30) & (r >= 0.1), 1, 0)))
    return lvl.where(~_monsoon(d), 0).where(g.notnull() & r.notnull())


def seven_day_level(flag: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray]:
    index = flag.astype(int).rolling(time=7, min_periods=7).sum()
    level = xr.where(index >= 5, 3, xr.where(index >= 3, 2, xr.where(index >= 1, 1, 0)))
    return index, level.where(index.notnull())


def _registry() -> dict:
    return {**DAILY_RULES, **EVENT_RULES, **LEVEL_RULES}


def rule_vars(rule_ids: list[str]) -> list[str]:
    reg = _registry()
    return sorted({v for r in rule_ids if r in reg for v in reg[r]["vars"]})


# =============================================================================
# Data
# =============================================================================

def months_between(first: date, last: date) -> list[tuple[int, int]]:
    out, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def event_months(ev) -> list[tuple[int, int]]:
    reg = _registry()
    first = ev["start"] - timedelta(days=LEAD_DAYS)
    if any(reg.get(r, {}).get("from_jan1") for r in ev["rules"]):
        first = min(first, date(first.year, 1, 1))
    for r in ev["rules"]:
        # Rules that count from a fixed date (H05: the monsoon from 8 June) need data from then.
        if "from_date" in reg.get(r, {}):
            m, d = reg[r]["from_date"]
            first = min(first, date(first.year, m, d))
    # 8 extra days before: a complete 7-day index on the first day, and the
    # first IST day starts at 18:30 UTC the day before.
    return months_between(first - timedelta(days=8), ev["end"] + timedelta(days=1))


def _raw_file(short: str, y: int, m: int, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / short / f"{short}_{y}_{m:02d}.nc"


# Months bundled into one CDS request. Almost all of a request's time is spent
# queued on the CDS side, so fewer, larger requests finish sooner; a bundle CDS
# refuses falls back to single months (see _fetch).
MONTHS_PER_REQUEST = 3


def _fetch(dataset: str, y: int, months: list[int], shorts: list[str], logger,
          raw_dir: Path = RAW_DIR, area: list[float] = AREA, retries_left: int = 4) -> tuple[str, bool]:
    """One CDS request for some variables over one or more months of one year,
    split into one file per variable per month. CDS returns a zip when the
    variables span accumulated and instantaneous fields; every NetCDF inside
    is split the same way."""
    import cdsapi

    label = f"{y}-{'/'.join(f'{m:02d}' for m in months)} [{', '.join(shorts)}]"
    # Staging goes in the system temp folder, created with a plain mkdir:
    # tempfile.mkdtemp's owner-only permissions left empty staging folders in
    # the OneDrive project folder that could not be deleted afterwards.
    tmp_dir = Path(tempfile.gettempdir()) / f"era5_{y}_{months[0]:02d}_{shorts[0]}_{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        logger.info("Requesting %s", label)
        target = tmp_dir / "download"
        req = {
            "product_type": "reanalysis", "format": "netcdf",
            "variable": [CDS_NAMES[s] for s in shorts],
            "year": str(y), "month": [f"{m:02d}" for m in months],
            "day": [f"{d:02d}" for d in range(1, 32)],   # CDS skips dates that do not exist
            "time": pl.ALL_HOURS,
            "area": GUST_AREA if dataset != DATASET else area,
        }
        cdsapi.Client(quiet=True, progress=False).retrieve(dataset, req, str(target))
        if zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as zf:
                zf.extractall(tmp_dir / "unzipped")
            parts = sorted((tmp_dir / "unzipped").rglob("*.nc"))
        else:
            parts = [target]
        for part in parts:
            with xr.open_dataset(part) as ds:
                tdim = "valid_time" if "valid_time" in ds.dims else "time"
                for s in shorts:
                    if s not in ds.data_vars:
                        continue
                    for m in months:
                        sub = ds[[s]].sel({tdim: (ds[tdim].dt.year == y) & (ds[tdim].dt.month == m)})
                        out = _raw_file(s, y, m, raw_dir)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        # Write under a temporary name first so an interrupted run never
                        # leaves a partial file that would later be taken as complete.
                        sub.load().to_netcdf(out.with_suffix(".part"))
                        out.with_suffix(".part").replace(out)
        missing = [f"{s}_{y}_{m:02d}" for s in shorts for m in months if not _raw_file(s, y, m, raw_dir).exists()]
        if missing:
            raise RuntimeError(f"missing from the CDS response: {missing}")
        logger.info("Done %s", label)
        return label, True
    except Exception as e:
        logger.exception("Failed %s", label)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        msg = str(e)
        if "temporarily limited" in msg or "queued requests" in msg:
            # A rate limit, not a size problem -- CDS is asking us to submit less
            # often, not to shrink the request. Splitting into more requests here
            # would make this worse (it did -- a splitting cascade tripped this
            # exact limit once already), so this waits and resubmits the SAME
            # request unchanged, backing off further each time it recurs.
            if retries_left <= 0:
                logger.error("Giving up on %s: still rate-limited after all retries", label)
                return label, False
            wait_s = 60 * (5 - retries_left)  # 60, 120, 180, 240s
            logger.info("Rate-limited on %s -- waiting %ds before resubmitting unchanged", label, wait_s)
            time.sleep(wait_s)
            return _fetch(dataset, y, months, shorts, logger, raw_dir, area, retries_left - 1)
        if len(months) > 1:
            # CDS may refuse a multi-month request as too large: fall back to one month at a time.
            logger.info("Retrying %s one month at a time", label)
            results = []
            for m in months:
                time.sleep(2)  # a brief gap between fallback submissions, not just on the rate-limit path
                results.append(_fetch(dataset, y, [m], shorts, logger, raw_dir, area, retries_left))
            return label, all(ok for _, ok in results)
        if len(shorts) > 1:
            # Still too large even at one month (e.g. a wide area with all 7 variables):
            # split the variable list in half and retry each half as its own request.
            mid = len(shorts) // 2
            logger.info("Retrying %s split into %s + %s", label, shorts[:mid], shorts[mid:])
            r1 = _fetch(dataset, y, months, shorts[:mid], logger, raw_dir, area, retries_left)
            time.sleep(2)
            r2 = _fetch(dataset, y, months, shorts[mid:], logger, raw_dir, area, retries_left)
            return label, r1[1] and r2[1]
        return label, False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def download(variables: list[str], months: list[tuple[int, int]],
            raw_dir: Path = RAW_DIR, area: list[float] = AREA, parallel: int | None = None) -> None:
    logger = pl.get_logger("download", LOG_DIR)
    raw_dir.mkdir(parents=True, exist_ok=True)
    total = len(variables) * len(months)
    jobs = []
    for dataset in sorted({DATASETS.get(s, DATASET) for s in variables}):
        ds_vars = [s for s in variables if DATASETS.get(s, DATASET) == dataset]
        # Group consecutive months of the same year that miss the same variables.
        groups: list[tuple[int, list[int], list[str]]] = []
        for y, m in months:
            need = [s for s in ds_vars if not _raw_file(s, y, m, raw_dir).exists()]
            if not need:
                continue
            g = groups[-1] if groups else None
            if g and g[0] == y and g[2] == need and m == g[1][-1] + 1 and len(g[1]) < MONTHS_PER_REQUEST:
                g[1].append(m)
            else:
                groups.append((y, [m], need))
        jobs += [(dataset, y, ms, need) for y, ms, need in groups]
    if not jobs:
        logger.info("All %d month-files already downloaded", total)
        return
    todo = sum(len(ms) * len(need) for _, _, ms, need in jobs)
    logger.info("Need %d of %d month-files (%d already present) -> %d request(s)",
                todo, total, total - todo, len(jobs))
    with ThreadPoolExecutor(max_workers=parallel or _DEFAULTS.get("cds_parallel", 4)) as ex:
        results = list(ex.map(lambda j: _fetch(j[0], j[1], j[2], j[3], logger, raw_dir, area), jobs))
    failed = [label for label, ok in results if not ok]
    if failed:
        raise RuntimeError(f"Downloads failed: {failed}")


def load_hourly(short: str, months: list[tuple[int, int]], raw_dir: Path = RAW_DIR) -> xr.DataArray:
    files = [raw_dir / short / f"{short}_{y}_{m:02d}.nc" for y, m in months]
    ds = xr.open_mfdataset([str(f) for f in files], combine="by_coords")
    if "valid_time" in ds.dims:
        ds = ds.rename(valid_time="time")
    da = ds[short].load()
    drop = [c for c in ("number", "expver") if c in da.coords]
    return da.drop_vars(drop).sortby("time")


def _e0_kpa(t):  # FAO-56 Eq. 11
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def fao56_et0(d: xr.Dataset) -> xr.DataArray:
    """FAO-56 Penman-Monteith reference ET (mm/day), Eq. 6, daily step."""
    tmax, tmin = d["tmax"], d["tmin"]
    t = (tmax + tmin) / 2
    es = (_e0_kpa(tmax) + _e0_kpa(tmin)) / 2
    ea = _e0_kpa(d["tdew"])
    delta = 4098 * _e0_kpa(t) / (t + 237.3) ** 2
    gamma = 0.665e-3 * d["p_kpa"]
    u2 = d["u10"] * 4.87 / np.log(67.8 * 10 - 5.42)
    # Extraterrestrial and clear-sky radiation, Eqs. 21-25 and 37.
    phi = np.deg2rad(d["latitude"])
    j = d["time"].dt.dayofyear
    dr = 1 + 0.033 * np.cos(2 * np.pi * j / 365)
    dec = 0.409 * np.sin(2 * np.pi * j / 365 - 1.39)
    ws = np.arccos(-np.tan(phi) * np.tan(dec))
    ra = 24 * 60 / np.pi * 0.0820 * dr * (ws * np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.sin(ws))
    z = (1 - (d["p_kpa"] / 101.3) ** (1 / 5.26)) * 293 / 0.0065   # Eq. 7 inverted
    rso = (0.75 + 2e-5 * z) * ra
    rs = d["rs_mj"]
    # Eq. 39. Rs/Rso is bounded to 0.3-1.0 (ASCE-EWRI 2005, Eq. 45): on heavily
    # overcast monsoon days an unbounded ratio makes the cloud factor, and so
    # the net longwave loss, negative -- not physical.
    rnl = 4.903e-9 * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2 \
        * (0.34 - 0.14 * np.sqrt(ea)) * (1.35 * (rs / rso).clip(0.3, 1.0) - 0.35)
    rn = 0.77 * rs - rnl                                                           # Eqs. 38, 40
    et0 = (0.408 * delta * rn + gamma * 900 / (t + 273) * u2 * (es - ea)) / (delta + gamma * (1 + 0.34 * u2))
    return et0.clip(min=0)


def water_balance(rain: xr.DataArray, et0: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray]:
    """FAO-56 Eq. 85 root-zone depletion with the Eq. 84 stress coefficient.
    Returns (depletion Dr at end of day, 'wet day' = at field capacity and rain >= ETc)."""
    raw = P_DEPLETION * TAW_MM

    def step(p, e):
        dr_out = np.empty_like(p)
        wet = np.zeros_like(p, dtype=bool)
        dr = TAW_MM
        for i in range(len(p)):
            ks = 1.0 if dr <= raw else max(0.0, (TAW_MM - dr) / (TAW_MM - raw))
            etc = ks * KC_CITRUS * e[i]
            dr = min(TAW_MM, max(0.0, dr - p[i] + etc))
            dr_out[i] = dr
            wet[i] = (dr == 0.0) and (p[i] >= etc)
        return dr_out, wet

    dr, wet = xr.apply_ufunc(step, rain.fillna(0), et0.fillna(0), input_core_dims=[["time"], ["time"]],
                             output_core_dims=[["time"], ["time"]], vectorize=True)
    return dr.transpose(*rain.dims), wet.transpose(*rain.dims)


def _deaccumulated(short: str, months, raw_dir: Path = RAW_DIR) -> xr.DataArray:
    """Hourly amounts from an ERA5-Land accumulated field (tp, ssrd).

    ERA5-Land accumulates from 00 UTC: the 01 UTC value is the first hour's
    amount and each later value (through 00 UTC next day) is the running total.
    The reset hour is fixed, so it is used directly. pipeline.deaccumulate
    instead treats *any* decrease as a reset, and the stored data has tiny
    rounding dips in the flat night-time plateau (checked on Jan-Feb 2020:
    1381 for ssrd, 2044 for tp outside 01 UTC), each of which re-added a whole
    day's total. Clipping those dips to 0 would bias every day upwards (up to
    0.9 mm/day for tp), so the running total is first made non-decreasing
    within each accumulation cycle (running maximum); differences of that
    are >= 0 and still sum to the cycle's total."""
    da = load_hourly(short, months, raw_dir)
    cycle = (da["time"].dt.hour == 1).cumsum("time")
    axis = da.get_axis_num("time")
    da = da.groupby(cycle).map(lambda g: g.copy(data=np.maximum.accumulate(g.values, axis=axis)))
    diff = da.diff("time")
    first_hour = diff["time"].dt.hour == 1
    hourly = xr.where(first_hour, da.isel(time=slice(1, None)), diff)
    # The very first timestep has no previous value; it falls on the day before
    # the downloaded range starts, which daily_fields drops as incomplete anyway.
    return hourly.transpose(*da.dims)


def daily_fields(variables: list[str], months: list[tuple[int, int]], raw_dir: Path = RAW_DIR) -> xr.Dataset:
    """Daily fields on Indian (IST) calendar days -- the incident dates are Indian
    dates, and a UTC day would split the Indian night across two days. Days with
    fewer than 24 hourly temperature values (the edges of the download) are dropped."""
    def daily(h, how):
        h = h.assign_coords(time=h["time"] + IST)
        return getattr(h.resample(time="1D"), how)()

    out = {}
    t = load_hourly("t2m", months, raw_dir) - 273.15
    full = daily(t, "count") == 24
    out["tmax"], out["tmin"], out["tmean"] = daily(t, "max"), daily(t, "min"), daily(t, "mean")
    out["dd_gp01"] = daily(t.clip(10.5, 33.0) - 10.5, "mean")
    out["dd_gp03"] = daily(t.clip(8.5, 27.5) - 8.5, "mean")
    out["dd_gp02"] = daily(t.clip(10.8, 32.0) - 10.8, "mean")
    out["dd_gp05"] = daily(t.clip(10.6, 33.0) - 10.6, "mean")
    if "d2m" in variables:
        td = load_hourly("d2m", months, raw_dir) - 273.15
        rh = (100 * pl._e_hpa(td) / pl._e_hpa(t)).clip(max=100)
        out["tdew"] = daily(td, "mean")
        out["vp_hpa"] = daily(pl._e_hpa(td), "mean")
        out["rh_mean"] = daily(rh, "mean")
        out["lw_hours"] = daily((rh >= LEAF_WET_RH).astype(int), "sum")
    rain_h = ws = None
    if "tp" in variables:
        rain_h = _deaccumulated("tp", months, raw_dir) * 1000.0
        out["rain_mm"] = daily(rain_h, "sum")
    if "u10" in variables:
        ws = np.hypot(load_hourly("u10", months, raw_dir), load_hourly("v10", months, raw_dir))
        out["u10"] = daily(ws, "mean")
    if rain_h is not None and ws is not None:
        # D02: hours with measurable rain (IMD trace 0.1 mm) and wind > 8 m/s together.
        wdr = (rain_h >= 0.1) & (ws.sel(time=rain_h["time"]) > 8.0)
        out["wdr_hours"] = daily(wdr.astype(int), "sum")
    if "fg10" in variables:
        g = load_hourly("fg10", months, raw_dir) * 3.6   # m/s -> km/h
        g = daily(g, "max")
        # 0.25° ERA5 points -> each 0.1° ERA5-Land cell takes its nearest gust point.
        out["gust_kmh"] = g.sel(latitude=t["latitude"], longitude=t["longitude"], method="nearest") \
                           .assign_coords(latitude=t["latitude"], longitude=t["longitude"])
    if "ssrd" in variables:
        out["rs_mj"] = daily(_deaccumulated("ssrd", months, raw_dir) / 1e6, "sum")
    if "sp" in variables:
        out["p_kpa"] = daily(load_hourly("sp", months, raw_dir) / 1000.0, "mean")

    ds = xr.Dataset(out).where(full)
    ds = ds.sel(time=full.any(["latitude", "longitude"]))
    if {"rain_mm", "rs_mj", "p_kpa", "tdew", "u10"} <= set(ds.data_vars):
        ds["et0_mm"] = fao56_et0(ds)
        ds["etc_mm"] = KC_CITRUS * ds["et0_mm"]
        ds["wb_depletion_mm"], ds["wb_wet_day"] = water_balance(ds["rain_mm"], ds["et0_mm"])
    return ds


# =============================================================================
# Evaluate one event
# =============================================================================

def _at(da: xr.DataArray, ev) -> xr.DataArray:
    return da.sel(latitude=ev["lat"], longitude=ev["lon"], method="nearest")


def _first_date(da: xr.DataArray):
    hit = da.where(da, drop=True)
    return pd.Timestamp(hit["time"].values[0]).date() if hit.size else None


def evaluate(event_id: str) -> None:
    events, thresholds = read_workbook()
    if event_id not in events.index:
        raise SystemExit(f"Unknown event {event_id}. Known: {', '.join(events.index)}")
    ev = events.loc[event_id]
    win_start, win_end = ev["start"] - timedelta(days=LEAD_DAYS), ev["end"]
    win = slice(str(win_start), str(win_end))
    out_dir = EVENTS_DIR / event_id
    out_dir.mkdir(parents=True, exist_ok=True)

    daily_ids = [r for r in ev["rules"] if r in DAILY_RULES]
    level_ids = [r for r in ev["rules"] if r in LEVEL_RULES]
    event_ids = [r for r in ev["rules"] if r in EVENT_RULES]
    cal_ids = [r for r in ev["rules"] if r in CALENDAR_RULES]
    deferred = [r for r in ev["rules"] if r in DEFERRED_RULES]
    missing = [r for r in ev["rules"] if r not in _registry() and r not in CALENDAR_RULES
               and r not in DEFERRED_RULES]

    print(f"\n=== {event_id}: {ev['Pest / disease / stress']} ===")
    print(f"Reported:   {ev['What was reported']}")
    print(f"Incident:   {ev['start']} to {ev['end']}   cell {ev['cell']}")
    print(f"Checked:    {win_start} to {win_end}  ({LEAD_DAYS} days lead-in)")

    rows, strips, maps = [], {}, {}
    table = None
    def record_levels(r, level, kind):
        lv = _at(level, ev).sel(time=win)
        table[f"{r}_level"] = lv.to_series()
        strips[r] = ("level", lv.to_series())
        maps[r] = ("level", level.sel(time=win).max("time"))
        top = int(lv.max()) if lv.notnull().any() else 0
        first = _first_date(lv >= 1)
        rows.append({"rule": r, "type": kind, "alert": "Yes" if first else "No",
                     "first_alert": first, "lead_days": (ev["start"] - first).days if first else None,
                     "highest": LEVEL_NAMES[top], "first_at_highest": _first_date(lv == top) if top else None,
                     "days_Low": int((lv == 1).sum()), "days_Moderate": int((lv == 2).sum()),
                     "days_High": int((lv == 3).sum())})

    if daily_ids or event_ids or level_ids:
        months = event_months(ev)
        variables = rule_vars(daily_ids + event_ids + level_ids)
        download(variables, months)
        d = daily_fields(variables, months)
        table = _at(d, ev).drop_vars(["latitude", "longitude"]).to_dataframe().loc[str(win_start):str(win_end)]
        table = table[[c for c in table.columns if not c.startswith("dd_")]].round(2)

        for r in daily_ids:
            flag = DAILY_RULES[r]["fn"](d)
            index, level = seven_day_level(flag)
            table[f"{r}_pass"] = _at(flag, ev).sel(time=win).to_series().astype(int)
            table[f"{r}_index7"] = _at(index, ev).sel(time=win).to_series()
            record_levels(r, level, "7-day index")

        for r in level_ids:
            record_levels(r, LEVEL_RULES[r]["fn"](d), "event rule (levels)")

        for r in event_ids:
            flag = EVENT_RULES[r]["fn"](d)
            f = _at(flag, ev).sel(time=win)
            table[f"{r}_alert"] = f.to_series().astype(int)
            strips[r] = ("event", f.to_series().astype(int))
            maps[r] = ("count", flag.sel(time=win).sum("time").where(d["tmax"].isel(time=0).notnull()))
            first = _first_date(f)
            rows.append({"rule": r, "type": "event rule", "alert": "Yes" if first else "No",
                         "first_alert": first, "lead_days": (ev["start"] - first).days if first else None,
                         "highest": "- (no levels)", "first_at_highest": None,
                         "days_Low": None, "days_Moderate": None, "days_High": int(f.sum()) or None})

    days = pd.date_range(win_start, win_end)
    for r in cal_ids:
        mons, from_day = CALENDAR_RULES[r]
        active = pd.Series([x.month in mons and not (from_day and x.month == mons[0] and x.strftime("%m-%d") < from_day)
                            for x in days], index=days).astype(int)
        strips[r] = ("calendar", active)
        first = active[active == 1].index.min()
        first = first.date() if pd.notna(first) else None
        rows.append({"rule": r, "type": "calendar", "alert": "Active" if first else "Not active",
                     "first_alert": first, "lead_days": (ev["start"] - first).days if first else None,
                     "highest": "-", "first_at_highest": None,
                     "days_Low": None, "days_Moderate": None, "days_High": None})

    result = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)
    print("\nPer-threshold result (lead_days = days before incident start; negative = after it began):")
    print(result.to_string(index=False))
    if table is not None and (ev["end"] - ev["start"]).days > 62:
        monthly = monthly_summary(table, daily_ids + level_ids, event_ids)
        print("\nAlert days per month at the event cell (7-day rules: days at Low or above):")
        print(monthly.to_string())
        monthly.to_csv(out_dir / f"{event_id}_monthly.csv")
    if deferred:
        print(f"\nDeferred to the anomaly stage (need the 1991-2020 normal / bias correction): {', '.join(deferred)}")
    if missing:
        print(f"Not implemented yet: {', '.join(missing)}")

    # Each weather rule's own first-alert date (calendar rules excluded -- they're not
    # a trigger), used to mark exactly where each strip first turns on in the chart.
    rule_first_alert = {r["rule"]: r["first_alert"] for r in rows
                        if r["type"] != "calendar" and r["first_alert"] is not None}

    s, t, u, x = sheet_values(ev, result, deferred, missing)
    print("\nFor the sheet:")
    print(f"  S  Alert generated?  {s}")
    print(f"  T  First alert date  {t}")
    print(f"  U  Highest level     {u}")
    print(f"  X  Remarks           {x}")

    # Every output on its own, including the sheet: a file left open in Excel
    # or a chart error is reported and skipped, never losing the rest.
    outputs = [("Pandhurna-Backtest-RESULTS.xlsx", lambda: fill_sheet(event_id, s, t, u, x)),
               (f"{event_id}_result.csv", lambda: result.to_csv(out_dir / f"{event_id}_result.csv", index=False)),
               (f"{event_id}_rules_and_sources.md",
                lambda: write_sources(ev, event_id, thresholds, daily_ids + level_ids + event_ids + cal_ids,
                                      deferred, missing, out_dir))]
    if table is not None:
        outputs += [(f"{event_id}_daily.csv", lambda: table.to_csv(out_dir / f"{event_id}_daily.csv")),
                    (f"{event_id}_timeline.png",
                     lambda: plot_timeline(ev, event_id, table, strips, win_start, out_dir,
                                           overall_first_alert=t, rule_first_alert=rule_first_alert)),
                    (f"{event_id}_risk_map.png", lambda: plot_risk_map(ev, event_id, maps, win_start, out_dir))]
    for name, write in outputs:
        try:
            write()
        except PermissionError:
            print(f"Could not write {name} -- it is open in another program; close it and run again.")
        except Exception as e:  # noqa: BLE001 -- one bad chart must not stop the others
            print(f"Could not write {name}: {e!r}")
    print(f"\nFiles written to {out_dir}")
    return {"event": event_id, "S": s, "T": t, "U": u, "X": x}


def monthly_summary(table, daily_ids, event_ids) -> pd.DataFrame:
    t = table.copy()
    t.index = pd.to_datetime(t.index)
    cols = {r: (t[f"{r}_level"] >= 1).astype(int) for r in daily_ids}
    cols.update({r: t[f"{r}_alert"] for r in event_ids})
    if "rain_mm" in t:
        cols["rain_mm"] = t["rain_mm"]
    m = pd.DataFrame(cols).resample("MS").sum()
    m.index = m.index.strftime("%Y-%m")
    return m.round(1)


def sheet_values(ev, result, deferred, missing) -> tuple[str, str, str, str]:
    weather = result[result["type"] != "calendar"]
    fired = weather[weather["alert"] == "Yes"]
    s = "Yes" if len(fired) else "No"
    t = str(min(fired["first_alert"])) if len(fired) else "-"
    levels = [LEVEL_NAMES.index(h) for h in weather["highest"] if h in LEVEL_NAMES]
    u = LEVEL_NAMES[max(levels)] if levels and max(levels) > 0 else ("- (event rule only)" if len(fired) else "None")

    win_start = ev["start"] - timedelta(days=LEAD_DAYS)
    parts = [f"ERA5-Land 0.1°, cell {ev['cell']}, checked {win_start} to {ev['end']}."]
    for _, r in weather.iterrows():
        if r["alert"] == "Yes":
            lead = r["lead_days"]
            if r["first_alert"] == win_start:
                # An alert on the window's first day was already on before the check began --
                # not an early warning found LEAD_DAYS ahead.
                when = "already active when the check window opens"
            elif lead > 0:
                when = f"{lead} d before start"
            else:
                when = "on start day" if lead == 0 else f"{-lead} d after start"
            lvl = f", highest {r['highest']}" if r["highest"] in LEVEL_NAMES else f", {r['days_High']} alert days"
            parts.append(f"{r['rule']}: first alert {r['first_alert']} ({when}){lvl}.")
        else:
            parts.append(f"{r['rule']}: no alert.")
    cal = result[result["type"] == "calendar"]
    if len(cal):
        parts.append("Calendar (not counted as alert): " + "; ".join(
            f"{r['rule']} {'active from ' + str(r['first_alert']) if pd.notna(r['first_alert']) else 'not active'}"
            for _, r in cal.iterrows()) + ".")
    if deferred:
        parts.append(f"Pending anomaly stage: {', '.join(deferred)}.")
    if missing:
        parts.append(f"Not yet tested: {', '.join(missing)}.")
    return s, t, u, " ".join(parts)


# =============================================================================
# Outputs: results workbook, sources note, charts
# =============================================================================

COL_S, COL_T, COL_U, COL_X = 19, 20, 21, 24


def fill_sheet(event_id: str, s: str, t: str, u: str, x: str) -> None:
    import openpyxl
    if not RESULTS_XLSX.exists():
        _copy_workbook(RESULTS_XLSX)
    wb = openpyxl.load_workbook(RESULTS_XLSX)
    ws = wb.worksheets[0]
    for row in ws.iter_rows(min_row=4):
        if row[0].value == event_id:
            r = row[0].row
            ws.cell(r, COL_S, s)
            ws.cell(r, COL_T, t)
            ws.cell(r, COL_U, u)
            ws.cell(r, COL_X, x)
            break
    try:
        wb.save(RESULTS_XLSX)
        print(f"Filled row {event_id} (columns S, T, U, X) in {RESULTS_XLSX.name}")
    except PermissionError:
        print(f"Could not save {RESULTS_XLSX.name} -- close it in Excel and run again.")


def write_sources(ev, event_id, thresholds, tested, deferred, missing, out_dir) -> None:
    lines = [f"# {event_id} -- rules and where they come from", "",
             f"**Event:** {ev['Pest / disease / stress']}  ",
             f"**Reported:** {ev['What was reported']}  ",
             f"**Source of the event:** {ev['Source']} ({ev['URL / document']})  ",
             f"**Incident period:** {ev['start']} to {ev['end']}; checked from "
             f"{ev['start'] - timedelta(days=LEAD_DAYS)} ({LEAD_DAYS} days lead-in)  ",
             f"**Grid cell:** {ev['cell']} (ERA5-Land 0.1°, nearest cell centre)", "",
             "Everything under *From the workbook* is copied from sheet 2 of "
             "`280926-Pandhurna-Backtest- V2.xlsx`. *How it is computed* is our implementation "
             "of that wording; any choice the sheet does not fix is stated there.", "",
             "General choices for every rule:", "",
             "- Days are Indian calendar days (IST, UTC+5:30), built from ERA5-Land hourly data.",
             "- 7-day index = number of the last 7 days (including today) meeting the condition: "
             "High >= 5, Moderate >= 3, Low >= 1.",
             "- The alert columns in the sheet (S, T, U) count weather rules only; calendar rules are "
             "reported in the remarks.", ""]
    for r in tested + deferred + missing:
        row = thresholds.loc[r] if r in thresholds.index else None
        lines += [f"## {r} -- {row['Name'] if row is not None else '(not in sheet 2)'}", ""]
        if row is not None:
            lines += ["**From the workbook**", "",
                      f"- Rule set: {row['Rule set']}",
                      f"- Condition: {row['Condition (exactly as used)']}",
                      f"- Alert levels: {row['Alert levels']}",
                      f"- Source: {row['Source']}"]
            if pd.notna(row["Quotation from the source"]) and row["Quotation from the source"] != "see condition":
                lines.append(f"- Quotation: \"{row['Quotation from the source']}\"")
            if pd.notna(row["Evidence grade"]) and row["Evidence grade"] != "-":
                lines.append(f"- Evidence grade: {row['Evidence grade']}")
            if pd.notna(row["Notes / caveats"]):
                lines.append(f"- Notes / caveats: {row['Notes / caveats']}")
            lines.append("")
        impl = (_registry().get(r) or {}).get("impl")
        if impl:
            lines += ["**How it is computed**", "", impl, ""]
        elif r in CALENDAR_RULES:
            mons, from_day = CALENDAR_RULES[r]
            lines += ["**How it is computed**", "",
                      f"Calendar only: active in months {mons}" + (f" from {from_day}" if from_day else "")
                      + ". No weather data.", ""]
        elif r in deferred:
            lines += ["**Status:** deferred to the anomaly stage (needs the 1991-2020 normal or the IMD "
                      "Nagpur bias correction).", ""]
        else:
            lines += ["**Status:** not implemented yet.", ""]
    (out_dir / f"{event_id}_rules_and_sources.md").write_text("\n".join(lines), encoding="utf-8")


def plot_timeline(ev, event_id, table, strips, win_start, out_dir,
                  overall_first_alert: str | None = None, rule_first_alert: dict | None = None) -> None:
    rule_first_alert = rule_first_alert or {}
    t = pd.to_datetime(table.index)
    has_rain = "rain_mm" in table
    n_top = 2 if has_rain else 1
    ratios = [5] + ([2.5] if has_rain else []) + [0.8] * len(strips)
    fig, axes = plt.subplots(n_top + len(strips), 1, figsize=(11, 3.4 + 0.9 * has_rain + 0.38 * len(strips)),
                             sharex=True, gridspec_kw={"height_ratios": ratios, "hspace": 0.15},
                             facecolor=pl.SURFACE)
    # inc0/inc1: the exact reported-incident window from sheet 1 of the parent workbook
    # (Incident period - start/end) -- inc1 is one day past the end so the shading and the
    # boundary line both cover the last reported day fully, not stop partway through it.
    inc0, inc1 = pd.Timestamp(ev["start"]), pd.Timestamp(ev["end"]) + pd.Timedelta(days=1)
    alert_ts = None
    if overall_first_alert and overall_first_alert != "-":
        try:
            alert_ts = pd.Timestamp(overall_first_alert)
        except (ValueError, TypeError):
            alert_ts = None

    ax = axes[0]
    pl._style_axes(ax)
    ax.axvspan(inc0, inc1, color=pl.GRIDLINE, alpha=0.6, linewidth=0, zorder=0)
    ax.fill_between(t, table["tmin"], table["tmax"], color=pl.TEMP_COLOR, alpha=0.15, linewidth=0)
    ax.plot(t, table["tmax"], color=pl.TEMP_COLOR, linewidth=1.5)
    ax.plot(t, table["tmean"], color=pl.INK_SECONDARY, linewidth=1)
    ax.text(t[-1], table["tmax"].iloc[-1], "  Daily high", color=pl.INK_SECONDARY, fontsize=8, va="center")
    ax.text(t[-1], table["tmean"].iloc[-1], "  Daily average", color=pl.INK_SECONDARY, fontsize=8, va="center")
    for thr in sorted({DAILY_RULES[r]["tmax_line"] for r in strips if "tmax_line" in DAILY_RULES.get(r, {})}):
        ax.axhline(thr, color=pl.INK_MUTED, linewidth=1, linestyle=(0, (4, 3)))
        ax.text(t[0], thr, f"{thr} °C ", color=pl.INK_MUTED, fontsize=8, ha="right", va="center")
    ax.set_ylabel("°C", color=pl.INK_SECONDARY, fontsize=9)

    if has_rain:
        ax = axes[1]
        pl._style_axes(ax)
        ax.axvspan(inc0, inc1, color=pl.GRIDLINE, alpha=0.6, linewidth=0, zorder=0)
        ax.bar(t, table["rain_mm"], width=1.0, color=pl.RAIN_COLOR, linewidth=0)
        ax.set_ylabel("rain\nmm/day", color=pl.INK_SECONDARY, fontsize=8)

    level_cmap = ListedColormap(LEVEL_COLORS)
    edges = t.append(pd.DatetimeIndex([t[-1] + pd.Timedelta(days=1)]))
    event_labels, cal_present = set(), False
    for ax, (r, (kind, series)) in zip(axes[n_top:], strips.items()):
        v = series.reindex(t).fillna(0).to_numpy()[None, :]
        if kind == "level":
            ax.pcolormesh(edges, [0, 1], v, cmap=level_cmap, vmin=-0.5, vmax=3.5)
        else:
            color = EVENT_RULE_COLOR if kind == "event" else CALENDAR_COLOR
            ax.pcolormesh(edges, [0, 1], v, cmap=ListedColormap([LEVEL_COLORS[0], color]), vmin=-0.5, vmax=1.5)
            if kind == "event":
                event_labels.add(rule_label(r))
            else:
                cal_present = True
        # This rule's OWN first-alert date, marked right on its own strip -- separate
        # from the one bold vertical line below, which marks the EVENT's overall
        # first-alert date (the earliest of all these, the T column in the sheet).
        fa = rule_first_alert.get(r)
        if fa is not None and kind != "calendar":
            ax.plot([pd.Timestamp(fa)], [1.0], marker="v", markersize=6, color=pl.INK_PRIMARY,
                    clip_on=False, zorder=10)
        ax.set_yticks([])
        # What grey/colour/blue mean on this row is explained once, in the legend below --
        # repeating it on every row label made long rows clip off the left edge of the figure.
        ax.set_ylabel(rule_label(r), rotation=0, ha="right", va="center",
                      color=pl.INK_PRIMARY, fontsize=7.5)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(colors=pl.INK_MUTED, labelsize=8)

    # Reference lines through every panel, research-figure style: dashed = the reported
    # incident window's exact boundaries (from sheet 1 of the parent workbook); solid =
    # the event's overall first-alert date (T column) -- drawn last so they sit on top
    # of the coloured strips rather than under them.
    for ax in axes:
        ax.axvline(inc0, color=pl.INK_MUTED, linewidth=1, linestyle=(0, (5, 3)), zorder=8)
        if inc1 != inc0:
            ax.axvline(inc1, color=pl.INK_MUTED, linewidth=1, linestyle=(0, (5, 3)), zorder=8)
        if alert_ts is not None:
            ax.axvline(alert_ts, color=pl.INK_PRIMARY, linewidth=1.5, zorder=9)

    handles = [Patch(color=c, label=f"{n} risk") if n != "None" else Patch(color=c, label="No risk")
               for c, n in zip(LEVEL_COLORS, LEVEL_NAMES)]
    if event_labels:
        handles.append(Patch(color=EVENT_RULE_COLOR, label="Risk active (" + "; ".join(sorted(event_labels)) + ")"))
    if cal_present:
        handles.append(Patch(color=CALENDAR_COLOR, label="Normal season for this pest/disease (not a weather trigger)"))
    handles.append(plt.Line2D([], [], color=pl.INK_MUTED, linewidth=1, linestyle=(0, (5, 3)),
                              label="Incident window boundary (from source sheet)"))
    if alert_ts is not None:
        handles.append(plt.Line2D([], [], color=pl.INK_PRIMARY, linewidth=1.5, label="First alert, T (earliest of all rules)"))
    handles.append(plt.Line2D([], [], marker="v", color="none", markerfacecolor=pl.INK_PRIMARY,
                              markersize=6, label="That rule's own first-alert date"))
    fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False, fontsize=8,
               labelcolor=pl.INK_SECONDARY, bbox_to_anchor=(0.5, -0.02))

    # Fixed daily/weekly/monthly tick spacing instead of matplotlib's default, which
    # crowds illegibly on a short (~2-3 week) single-incident window like a hail report.
    span_days = (t[-1] - t[0]).days
    bottom_ax = axes[-1]
    if span_days <= 45:
        locator = mdates.DayLocator(interval=max(1, span_days // 8))
    else:
        locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
    bottom_ax.xaxis.set_major_locator(locator)
    bottom_ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    # Title + a research-caption-style subtitle giving the exact incident dates (as
    # stated in the parent workbook) and the first-alert verdict -- kept as separate
    # figure-level text rather than an in-plot label, so it never collides with the
    # temperature curve or its line-end labels regardless of window length.
    fig.text(0.01, 0.985, f"{event_id} -- {ev['Pest / disease / stress']} (cell {ev['cell']})",
             fontsize=12, fontweight="bold", color=pl.INK_PRIMARY, ha="left", va="top")
    inc_label = (f"Incident: {ev['start']:%d %b %Y}" if ev["start"] == ev["end"] else
                f"Incident window: {ev['start']:%d %b %Y} – {ev['end']:%d %b %Y}")
    if alert_ts is not None:
        lead = (ev["start"] - alert_ts.date()).days
        when = (f"{lead} d before incident start" if lead > 0 else
                "on incident start day" if lead == 0 else f"{-lead} d after incident start")
        alert_label = f"First alert (T): {alert_ts:%d %b %Y} ({when})"
    else:
        alert_label = "First alert (T): none -- no weather rule triggered in this window"
    fig.text(0.01, 0.958, f"{inc_label}     |     {alert_label}",
             fontsize=8.5, color=pl.INK_SECONDARY, ha="left", va="top")

    fig.subplots_adjust(left=0.26, right=0.88, top=0.89, bottom=0.15 + 0.02 * (len(event_labels) + cal_present))
    fig.savefig(out_dir / f"{event_id}_timeline.png", dpi=150, facecolor=pl.SURFACE)
    plt.close(fig)


def plot_risk_map(ev, event_id, maps, win_start, out_dir) -> None:
    if not maps:
        return
    level_maps = {r: g for r, (k, g) in maps.items() if k == "level"}
    panels = dict(maps)
    if len(level_maps) > 1:
        panels["Worst of all risk rules"] = ("level", xr.concat(list(level_maps.values()), dim="rule").max("rule"))
    n = len(panels)
    ncols = min(n, 4)
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(3.3 * ncols, 3.4 * nrows + 0.8), facecolor=pl.SURFACE,
                             squeeze=False)
    level_cmap = ListedColormap(LEVEL_COLORS)
    norm = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], level_cmap.N)
    try:
        boundary = pl.resolve_region(None, PROJECT_ROOT / "Pandhurna.kml", None, 0.0).polygon
    except Exception:
        boundary = None
    for ax in axes.flat[n:]:
        ax.axis("off")
    for ax, (name, (kind, g)) in zip(axes.flat, panels.items()):
        ax.set_facecolor(pl.SURFACE)
        if kind == "level":
            ax.pcolormesh(g["longitude"], g["latitude"], g.values, cmap=level_cmap, norm=norm, shading="nearest",
                          edgecolors=pl.SURFACE, linewidth=0.3)
            title = rule_label(name) if name in RULE_LABEL else name
        else:
            mesh = ax.pcolormesh(g["longitude"], g["latitude"], g.values, cmap=pl.SEQ_BLUE, shading="nearest",
                                 edgecolors=pl.SURFACE, linewidth=0.3, vmin=0)
            cb = fig.colorbar(mesh, ax=ax, shrink=0.7, pad=0.02)
            cb.outline.set_visible(False)
            cb.ax.tick_params(colors=pl.INK_MUTED, labelsize=7)
            title = f"{rule_label(name)} -- days at risk"
        if boundary is not None:
            for poly in getattr(boundary, "geoms", [boundary]):
                xs, ys = poly.exterior.xy
                ax.plot(xs, ys, color=pl.INK_PRIMARY, linewidth=1.2)
        ax.plot(ev["lon"], ev["lat"], marker="o", markersize=8, markerfacecolor="none",
                markeredgecolor=pl.INK_PRIMARY, markeredgewidth=2)
        ax.set_title(title, fontsize=10, fontweight="bold", loc="left", color=pl.INK_PRIMARY)
        ax.set_aspect("equal")
        ax.tick_params(colors=pl.INK_MUTED, labelsize=7)
        for s in ax.spines.values():
            s.set_visible(False)
    handles = [Patch(color=c, label=(f"{nm} risk" if nm != "None" else "No risk")) for c, nm in zip(LEVEL_COLORS, LEVEL_NAMES)]
    handles.append(plt.Line2D([], [], marker="o", color="none", markeredgecolor=pl.INK_PRIMARY,
                              markeredgewidth=2, markersize=8, label="Reported location"))
    handles.append(plt.Line2D([], [], color=pl.INK_PRIMARY, linewidth=1.2, label="Pandhurna boundary"))
    fig.legend(handles=handles, loc="lower center", ncol=3, frameon=False, fontsize=8,
               labelcolor=pl.INK_SECONDARY, bbox_to_anchor=(0.5, 0.0))
    fig.suptitle(f"{event_id} -- worst risk level reached anywhere on the map, {win_start} to {ev['end']}\n"
                 f"(colour = highest level reached at any point in that square during this window, not how long it lasted)",
                 fontsize=11, fontweight="bold", x=0.01, ha="left", color=pl.INK_PRIMARY)
    fig.tight_layout(rect=(0, 0.1, 1, 0.9), h_pad=2.5)
    fig.savefig(out_dir / f"{event_id}_risk_map.png", dpi=150, facecolor=pl.SURFACE)
    plt.close(fig)


# =============================================================================
# Spatial multi-year mode: one event's rules, every grid cell, several full
# calendar years -- not tied to that event's own reported incident window.
# Manager's extension request: compare alert behaviour year over year, across
# all of Pandhurna, not just the one reported point.
# =============================================================================

def spatial_multiyear(event_id: str, start_year: int, end_year: int, lead_months: int = 6) -> None:
    events, _ = read_workbook()
    ev = events.loc[event_id]
    daily_ids = [r for r in ev["rules"] if r in DAILY_RULES]
    level_ids = [r for r in ev["rules"] if r in LEVEL_RULES]
    event_ids = [r for r in ev["rules"] if r in EVENT_RULES]
    cal_ids = [r for r in ev["rules"] if r in CALENDAR_RULES]  # calendar-only: shown on the timeline for context, no weather data needed
    variables = rule_vars(daily_ids + level_ids + event_ids)
    if not (daily_ids or level_ids or event_ids):
        raise SystemExit(f"{event_id} has no weather rules to run spatially (calendar-only rules skipped).")

    # lead_months of real data before start_year, so the waterlogging water-balance
    # (X-W01) has settled before the first year we actually report on -- same
    # principle as the per-event lead-in, just longer since this covers full years.
    lead_start = date(start_year, 1, 1) - timedelta(days=30 * lead_months)
    months = months_between(date(lead_start.year, lead_start.month, 1), date(end_year, 12, 1))

    out_dir = SPATIAL_DIR / event_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Pandhurna-only box (not WIDE_AREA/SPATIAL_RAW_DIR, which covered Chhindwara
    # too) -- north edge bumped to 22.0 from AREA's 21.8 to fully contain
    # Pandhurna's real boundary (KML bounds go up to lat 21.913); downloaded into
    # RAW_DIR (era5_raw), same folder the per-event runs use, since file-exists
    # checks key on filename only, not on which box a file was fetched under.
    area = [22.0, AREA[1], AREA[2], AREA[3]]
    print(f"{event_id} spatial {start_year}-{end_year}: rules {daily_ids + level_ids + event_ids}, "
          f"{len(variables)} variables x {len(months)} months ({months[0]} to {months[-1]}), box {area}")

    download(variables, months, raw_dir=RAW_DIR, area=area, parallel=4)
    d = daily_fields(variables, months, raw_dir=RAW_DIR)

    # Clip to Pandhurna's actual boundary -- the wide box also covers Narkhed/Katol/
    # Warud/Nagpur (kept only because those are the other events' locations); the
    # manager asked specifically for Pandhurna.
    boundary = None
    try:
        region = pl.resolve_region(None, PROJECT_ROOT / "Pandhurna.kml", None, 0.0)
        d = pl.clip_to_polygon(d, region)
        boundary = region.polygon
    except Exception as e:
        print(f"Could not clip to the Pandhurna boundary ({e!r}) -- using the full downloaded box instead.")

    # daily_flags/rolling_index kept (not discarded) for daily_ids specifically, so the
    # Excel export can show the full chain -- daily pass -> 7-day rolling count -> level
    # -- not just the final level, which on its own hides how it was reached.
    levels, event_flags, daily_flags, rolling_index = {}, {}, {}, {}
    for r in daily_ids:
        daily_flags[r] = DAILY_RULES[r]["fn"](d)
        rolling_index[r], levels[r] = seven_day_level(daily_flags[r])
    for r in level_ids:
        levels[r] = LEVEL_RULES[r]["fn"](d)
    for r in event_ids:
        event_flags[r] = EVENT_RULES[r]["fn"](d)

    years = list(range(start_year, end_year + 1))
    rows = []
    for year in years:
        win = slice(f"{year}-01-01", f"{year}-12-31")
        for r, lvl in levels.items():
            yr = lvl.sel(time=win)
            rows.append({
                "rule": r, "year": year,
                "avg_days_Low_across_Pandhurna": round(float((yr == 1).sum("time").mean(["latitude", "longitude"])), 1),
                "avg_days_Moderate_across_Pandhurna": round(float((yr == 2).sum("time").mean(["latitude", "longitude"])), 1),
                "avg_days_High_across_Pandhurna": round(float((yr == 3).sum("time").mean(["latitude", "longitude"])), 1),
                "max_days_High_anywhere_in_Pandhurna": int((yr == 3).sum("time").max()),
            })
        for r, flg in event_flags.items():
            yr = flg.sel(time=win)
            rows.append({
                "rule": r, "year": year,
                "avg_days_Low_across_Pandhurna": None, "avg_days_Moderate_across_Pandhurna": None,
                "avg_days_High_across_Pandhurna": round(float(yr.sum("time").mean(["latitude", "longitude"])), 1),
                "max_days_High_anywhere_in_Pandhurna": int(yr.sum("time").max()),
            })
    summary = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print("\nYear-by-year comparison (spatial average and worst cell, across Pandhurna):")
    print(summary.to_string(index=False))
    summary.to_csv(out_dir / f"{event_id}_spatial_{start_year}_{end_year}_summary.csv", index=False)

    plot_spatial_years(event_id, start_year, end_year, levels, event_flags, boundary, out_dir)
    plot_spatial_timeline(event_id, ev, d, levels, event_flags, cal_ids, start_year, end_year, out_dir)
    write_spatial_daily_excel(event_id, ev, d, daily_ids, level_ids, levels, daily_flags, rolling_index,
                              event_flags, cal_ids, start_year, end_year, out_dir)
    print(f"\nFiles written to {out_dir}")


def plot_spatial_years(event_id, start_year, end_year, levels, event_flags, boundary, out_dir) -> None:
    """Research-figure conventions throughout: real lat/lon axes (shared, shown only
    on the outer edge panels, like a journal multi-panel map), a thin frame on every
    panel instead of no border, integer-only colourbar ticks since a day count can't
    be fractional, and inward tick marks -- a spatial map with no coordinates and no
    frame reads as a decorative graphic, not a figure that could go in a paper."""
    from matplotlib.ticker import MaxNLocator
    years = list(range(start_year, end_year + 1))
    rules = list(levels) + list(event_flags)
    nrows, ncols = len(rules), len(years)
    fig, axes = plt.subplots(nrows, ncols, figsize=(2.6 * ncols + 1.2, 2.5 * nrows + 0.6), facecolor=pl.SURFACE,
                             squeeze=False)
    lon_fmt = lambda v, _: f"{v:.2f}°E"
    lat_fmt = lambda v, _: f"{v:.2f}°N"
    # Level rules: ONE map per year, categorical colour = highest level that cell
    # reached that year (same 4-colour None/Low/Moderate/High scheme as the single-
    # incident risk map, with one shared legend) -- not three separate day-count
    # maps. A day-count map answers "how many days" but a manager scanning a figure
    # wants "how bad, where" at a glance, which a category map gives directly; the
    # exact day counts are still in the summary CSV/sheet for anyone who wants them.
    level_cmap = ListedColormap(LEVEL_COLORS)
    level_norm = BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], level_cmap.N)
    for i, r in enumerate(rules):
        is_level = r in levels
        if is_level:
            yearly = [levels[r].sel(time=slice(f"{y}-01-01", f"{y}-12-31")).max("time") for y in years]
        else:
            yearly = [event_flags[r].sel(time=slice(f"{y}-01-01", f"{y}-12-31")).sum("time") for y in years]
        vmax = 1 if is_level else max((float(g.max()) for g in yearly if g.notnull().any()), default=1) or 1
        mesh = None
        for j, (y, g) in enumerate(zip(years, yearly)):
            ax = axes[i, j]
            ax.set_facecolor(pl.SURFACE)
            if is_level:
                ax.pcolormesh(g["longitude"], g["latitude"], g.values, cmap=level_cmap, norm=level_norm,
                             shading="nearest", edgecolors=pl.SURFACE, linewidth=0.2)
            else:
                mesh = ax.pcolormesh(g["longitude"], g["latitude"], g.values, cmap=pl.SEQ_BLUE, shading="nearest",
                                     edgecolors=pl.SURFACE, linewidth=0.2, vmin=0, vmax=vmax)
            if boundary is not None:
                for poly in getattr(boundary, "geoms", [boundary]):
                    xs, ys = poly.exterior.xy
                    ax.plot(xs, ys, color=pl.INK_PRIMARY, linewidth=1.2)
            if i == 0:
                ax.set_title(str(y), fontsize=10.5, fontweight="bold", color=pl.INK_PRIMARY)
            ax.set_ylabel(rule_label(r) if j == 0 else "", fontsize=8, color=pl.INK_PRIMARY, rotation=0,
                         ha="right", va="center", labelpad=12)
            ax.xaxis.set_major_locator(MaxNLocator(3))
            ax.yaxis.set_major_locator(MaxNLocator(3))
            if j == 0:
                ax.yaxis.set_major_formatter(lat_fmt)
                ax.tick_params(axis="y", labelsize=6.5, colors=pl.INK_MUTED)
            else:
                ax.set_yticklabels([])
                ax.tick_params(axis="y", length=0)
            if i == nrows - 1:
                ax.xaxis.set_major_formatter(lon_fmt)
                ax.tick_params(axis="x", labelsize=6.5, colors=pl.INK_MUTED, rotation=30)
            else:
                ax.set_xticklabels([])
                ax.tick_params(axis="x", length=0)
            ax.tick_params(direction="in", colors=pl.INK_MUTED)
            for s in ax.spines.values():
                s.set_visible(True)
                s.set_color(pl.INK_MUTED)
                s.set_linewidth(0.6)
        if not is_level and mesh is not None:
            cb = fig.colorbar(mesh, ax=axes[i, -1], shrink=0.85, pad=0.03, ticks=MaxNLocator(integer=True))
            cb.set_label("days active", fontsize=7.5, color=pl.INK_SECONDARY)
            cb.ax.tick_params(labelsize=7, colors=pl.INK_MUTED)
            cb.outline.set_linewidth(0.6)
            cb.outline.set_edgecolor(pl.INK_MUTED)
    # .any("time"), not .isel(time=0): a level rule is NaN everywhere for its first
    # ~6 days (the 7-day rolling window has no full week yet), which isn't the same
    # thing as a cell being outside the boundary -- day 0 alone undercounts real cells.
    n_cells = int(levels[rules[0]].notnull().any("time").sum()) if rules[0] in levels else \
        int(event_flags[rules[0]].notnull().any("time").sum())
    title = f"{event_id} -- {rule_label(rules[0]) if len(rules) == 1 else 'weather rules'} across Pandhurna, {start_year}-{end_year}"
    subtitle = f"Colour = highest risk level reached per 0.1° grid cell per year, n={n_cells} cells."
    fig.suptitle(title, fontsize=12, fontweight="bold", x=0.02, y=0.995, ha="left", va="top", color=pl.INK_PRIMARY)
    fig.text(0.02, 0.95, subtitle, fontsize=9, ha="left", va="top", color=pl.INK_SECONDARY)
    if any(r in levels for r in rules):
        handles = [Patch(color=c, label=(f"{n} risk" if n != "None" else "No risk"))
                  for c, n in zip(LEVEL_COLORS, LEVEL_NAMES)]
        fig.legend(handles=handles, loc="lower center", ncol=4, frameon=False, fontsize=9,
                  labelcolor=pl.INK_SECONDARY, bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.04, 1, 0.88))
    fig.savefig(out_dir / f"{event_id}_spatial_{start_year}_{end_year}_heatmaps.png", dpi=200, facecolor=pl.SURFACE)
    plt.close(fig)


def plot_spatial_timeline(event_id: str, ev, d: xr.Dataset, levels: dict, event_flags: dict, cal_ids: list[str],
                          start_year: int, end_year: int, out_dir: Path) -> None:
    """Companion to the spatial heatmaps: ONE continuous Jan start_year - Dec end_year
    timeline (not split into a block per year -- tried that first, but scanning 4
    separate stacked blocks to compare timing across years was harder to read than
    one continuous strip, the same complaint the single-incident timeline would get
    if it were cut into pieces). Temperature/rain are the spatial mean across
    Pandhurna; a rule's strip height is the FRACTION of Pandhurna's area at each
    level that day (stacked) -- rounding the area-mean level to one colour very
    rarely shows anything, since that only fires when most of the district is at the
    same level simultaneously, and the usual real pattern is part of the district
    affected while the rest isn't. Calendar-only rules (no weather data, e.g. a
    pest's normal season) are included too, exactly as the single-incident timeline
    shows them, so all of the event's rules appear here even the non-weather ones.
    The reported incident window (from sheet 1) is shaded for reference, same as
    the single-incident timeline, so you can see how it sits against the full
    4-year run."""
    daily_ids_order = list(levels) + list(event_flags) + list(cal_ids)
    tmax = d["tmax"].mean(["latitude", "longitude"])
    tmin = d["tmin"].mean(["latitude", "longitude"])
    tmean = d["tmean"].mean(["latitude", "longitude"])
    rain = d["rain_mm"].mean(["latitude", "longitude"]) if "rain_mm" in d else None
    t = pd.to_datetime(tmax["time"].values)
    full_days = pd.date_range(t[0], t[-1])

    strip_series = {}
    for r in levels:
        n_cells = levels[r].notnull().sum(["latitude", "longitude"])
        fracs = [((levels[r] == lvl).sum(["latitude", "longitude"]) / n_cells).reindex(time=full_days).fillna(0).values
                for lvl in (1, 2, 3)]
        strip_series[r] = ("level", fracs)
    for r in event_flags:
        n_cells = event_flags[r].notnull().sum(["latitude", "longitude"])
        frac = (event_flags[r].sum(["latitude", "longitude"]) / n_cells).reindex(time=full_days).fillna(0).values
        strip_series[r] = ("event", frac)
    for r in cal_ids:
        mons, from_day = CALENDAR_RULES[r]
        active = np.array([x.month in mons and not (from_day and x.month == mons[0]
                           and x.strftime("%m-%d") < from_day) for x in full_days], dtype=float)
        strip_series[r] = ("calendar", active)

    n_top = 2 if rain is not None else 1
    ratios = [3.0] + ([1.2] if rain is not None else []) + [0.5] * len(daily_ids_order)
    fig, axes = plt.subplots(n_top + len(daily_ids_order), 1, figsize=(15, 4.2 + 0.42 * len(daily_ids_order)),
                             sharex=True, gridspec_kw={"height_ratios": ratios, "hspace": 0.15},
                             facecolor=pl.SURFACE)
    axes = np.atleast_1d(axes)
    inc0, inc1 = pd.Timestamp(ev["start"]), pd.Timestamp(ev["end"]) + pd.Timedelta(days=1)

    ax = axes[0]
    pl._style_axes(ax)
    ax.axvspan(inc0, inc1, color=pl.GRIDLINE, alpha=0.8, linewidth=0, zorder=0)
    ax.fill_between(full_days, tmin.reindex(time=full_days).values, tmax.reindex(time=full_days).values,
                    color=pl.TEMP_COLOR, alpha=0.15, linewidth=0)
    ax.plot(full_days, tmax.reindex(time=full_days).values, color=pl.TEMP_COLOR, linewidth=1)
    ax.plot(full_days, tmean.reindex(time=full_days).values, color=pl.INK_SECONDARY, linewidth=0.8)
    ax.set_ylabel("°C", color=pl.INK_SECONDARY, fontsize=9)

    if rain is not None:
        ax = axes[1]
        pl._style_axes(ax)
        ax.axvspan(inc0, inc1, color=pl.GRIDLINE, alpha=0.8, linewidth=0, zorder=0)
        ax.bar(full_days, rain.reindex(time=full_days).fillna(0).values, width=1.0, color=pl.RAIN_COLOR, linewidth=0)
        ax.set_ylabel("rain\nmm/day", color=pl.INK_SECONDARY, fontsize=8)

    event_labels, cal_present = set(), False
    for ri, r in enumerate(daily_ids_order):
        ax = axes[n_top + ri]
        pl._style_axes(ax)
        ax.axvspan(inc0, inc1, color=pl.GRIDLINE, alpha=0.8, linewidth=0, zorder=0)
        ax.set_facecolor(LEVEL_COLORS[0])  # matches the "No risk" legend swatch, not the figure's own surface colour
        kind, series = strip_series[r]
        if kind == "level":
            ax.stackplot(full_days, series, colors=LEVEL_COLORS[1:], linewidth=0)
            hit = full_days[series[2] > 0]  # first day any part of Pandhurna reaches High
        elif kind == "event":
            ax.fill_between(full_days, 0, series, color=EVENT_RULE_COLOR, linewidth=0)
            event_labels.add(rule_label(r))
            hit = full_days[series > 0]
        else:
            ax.fill_between(full_days, 0, series, color=CALENDAR_COLOR, linewidth=0)
            cal_present = True
            hit = []
        ax.set_ylim(0, 1)
        if len(hit):
            ax.plot([hit[0]], [1.0], marker="v", markersize=6, color=pl.INK_PRIMARY, clip_on=False, zorder=10)
        ax.set_yticks([])
        ax.set_ylabel(rule_label(r), rotation=0, ha="right", va="center", color=pl.INK_PRIMARY, fontsize=8)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(colors=pl.INK_MUTED, labelsize=8)

    locator = mdates.AutoDateLocator(minticks=10, maxticks=18)
    axes[-1].xaxis.set_major_locator(locator)
    axes[-1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for ax in axes:
        ax.axvline(inc0, color=pl.INK_MUTED, linewidth=1, linestyle=(0, (5, 3)), zorder=8)
        if inc1 != inc0:
            ax.axvline(inc1, color=pl.INK_MUTED, linewidth=1, linestyle=(0, (5, 3)), zorder=8)

    handles = [Patch(color=c, label=f"{n} risk") if n != "None" else Patch(color=c, label="No risk")
              for c, n in zip(LEVEL_COLORS, LEVEL_NAMES)]
    if event_labels:
        handles.append(Patch(color=EVENT_RULE_COLOR, label="Risk active (" + "; ".join(sorted(event_labels)) + ")"))
    if cal_present:
        handles.append(Patch(color=CALENDAR_COLOR, label="Normal season for this pest/disease (not a weather trigger)"))
    handles.append(plt.Line2D([], [], color=pl.INK_MUTED, linewidth=1, linestyle=(0, (5, 3)),
                              label="Reported incident window (from source sheet)"))
    handles.append(plt.Line2D([], [], marker="v", color="none", markerfacecolor=pl.INK_PRIMARY,
                              markersize=6, label="First day any part of Pandhurna reaches High / fires"))
    fig.legend(handles=handles, loc="lower center", ncol=3, frameon=False, fontsize=8.5,
              labelcolor=pl.INK_SECONDARY, bbox_to_anchor=(0.5, -0.01))

    fig.text(0.02, 0.99, f"{event_id} -- {ev['Pest / disease / stress']}, {start_year}-{end_year} (whole Pandhurna)",
             fontsize=13, fontweight="bold", color=pl.INK_PRIMARY, ha="left", va="top")
    inc_range = (f"{ev['start']:%d %b %Y}" if ev['end'] == ev['start'] else
                f"{ev['start']:%d %b %Y} – {ev['end']:%d %b %Y}")
    fig.text(0.02, 0.965, f"Reported incident: {inc_range}",
            fontsize=9, color=pl.INK_SECONDARY, ha="left", va="top")
    fig.subplots_adjust(left=0.2, right=0.98, top=0.93, bottom=0.08 + 0.014 * len(daily_ids_order))
    fig.savefig(out_dir / f"{event_id}_spatial_{start_year}_{end_year}_timeline.png", dpi=150, facecolor=pl.SURFACE)
    plt.close(fig)


def write_spatial_daily_excel(event_id: str, ev, d: xr.Dataset, daily_ids: list[str], level_ids: list[str],
                              levels: dict, daily_flags: dict, rolling_index: dict, event_flags: dict,
                              cal_ids: list[str], start_year: int, end_year: int, out_dir: Path) -> None:
    """One row per day, start_year-01-01 to end_year-12-31: every weather variable
    computed for this run (spatial mean across Pandhurna), then each rule's full
    chain rather than just its final answer -- for a '7-day index' rule (daily_ids)
    that's did-it-pass-today -> how many of the last 7 days passed -> the resulting
    level; for an always-binary rule (event_ids), just the final active/not; for a
    rule built its own way (level_ids, e.g. dry-spell length), just its level, since
    there's no single generic intermediate step to show. Every rule's numbers use
    the SAME spatial convention throughout -- the worst/furthest-triggered value
    anywhere in Pandhurna that day -- so a column means the same thing across rows;
    the heatmap and timeline plots show the fuller area-extent picture that a single
    daily number can't. The reported incident window's rows (from sheet 1) are
    highlighted so it's easy to find inside four years of daily rows."""
    # dd_gp01/02/03/05 are intermediate degree-day accumulators for psylla/leaf-miner/
    # mealybug/fruit-fly timing rules (G-P01/02/03/05) -- computed unconditionally by
    # daily_fields() regardless of which rules this event actually uses, same as the
    # single-incident backtest, which also drops them from its own daily table for
    # the same reason: not interpretable on their own and just noise if unused here.
    skip = {"latitude", "longitude", "latitude_bnds", "longitude_bnds"}
    cols = {}
    for name, da in d.data_vars.items():
        if name in skip or name.startswith("dd_") or "time" not in da.dims:
            continue
        cols[name] = da.mean([dim for dim in ("latitude", "longitude") if dim in da.dims]).round(2).to_pandas()
    groups = []  # (group_label, [column names]) in display order, for the merged header row
    weather_cols = list(cols)
    groups.append(("Weather variables (spatial mean across Pandhurna)", list(weather_cols)))
    for r in daily_ids:
        cols[f"rule_{r}_daily_pass"] = daily_flags[r].max(["latitude", "longitude"]).astype(int).to_pandas()
        cols[f"rule_{r}_rolling7"] = rolling_index[r].max(["latitude", "longitude"]).to_pandas()
        lvl = levels[r].max(["latitude", "longitude"])
        cols[f"rule_{r}_level"] = lvl.to_pandas()
        cols[f"rule_{r}_level_name"] = lvl.to_pandas().map(lambda v: LEVEL_NAMES[int(v)] if pd.notna(v) else None)
        groups.append((f"{r} -- {rule_label(r)}", [f"rule_{r}_daily_pass", f"rule_{r}_rolling7",
                       f"rule_{r}_level", f"rule_{r}_level_name"]))
    for r in level_ids:
        lvl = levels[r].max(["latitude", "longitude"])
        cols[f"rule_{r}_level"] = lvl.to_pandas()
        cols[f"rule_{r}_level_name"] = lvl.to_pandas().map(lambda v: LEVEL_NAMES[int(v)] if pd.notna(v) else None)
        groups.append((f"{r} -- {rule_label(r)}", [f"rule_{r}_level", f"rule_{r}_level_name"]))
    for r in event_flags:
        cols[f"rule_{r}_active"] = event_flags[r].max(["latitude", "longitude"]).astype(int).to_pandas()
        groups.append((f"{r} -- {rule_label(r)}", [f"rule_{r}_active"]))
    df = pd.DataFrame(cols)
    df.index.name = "date"
    df = df.sort_index()
    for r in cal_ids:
        mons, from_day = CALENDAR_RULES[r]
        df[f"rule_{r}_normal_season"] = [int(x.month in mons and not (from_day and x.month == mons[0]
                                         and x.strftime("%m-%d") < from_day)) for x in df.index]
        groups.append((f"{r} -- {rule_label(r)} (calendar only)", [f"rule_{r}_normal_season"]))

    # Per-rule overview, same shape as the single-incident backtest's own _result.csv
    # (type / alert / first-trigger date / highest level reached, named not numbered /
    # day counts at each level) -- just over the full start_year-end_year run instead
    # of one incident's window, and "anywhere in Pandhurna" instead of one point.
    summary_rows = []
    for r in daily_ids + level_ids:
        s = df[f"rule_{r}_level"]
        first_alert = s[s >= 1].index.min() if (s >= 1).any() else None
        top = int(s.max()) if s.notnull().any() else 0
        first_at_top = s[s == top].index.min() if top else None
        summary_rows.append({"rule": r, "name": rule_label(r), "type": "7-day index" if r in daily_ids else "level rule",
                             "alert": "Yes" if first_alert is not None else "No", "first_alert": first_alert,
                             "highest_level": LEVEL_NAMES[top], "first_at_highest": first_at_top,
                             "days_Low": int((s == 1).sum()), "days_Moderate": int((s == 2).sum()),
                             "days_High": int((s == 3).sum())})
    for r in event_flags:
        s = df[f"rule_{r}_active"]
        first_alert = s[s == 1].index.min() if (s == 1).any() else None
        summary_rows.append({"rule": r, "name": rule_label(r), "type": "event rule",
                             "alert": "Yes" if first_alert is not None else "No", "first_alert": first_alert,
                             "highest_level": "- (no levels)", "first_at_highest": None,
                             "days_Low": None, "days_Moderate": None, "days_High": int(s.sum()) or None})
    for r in cal_ids:
        s = df[f"rule_{r}_normal_season"]
        first_active = s[s == 1].index.min() if (s == 1).any() else None
        summary_rows.append({"rule": r, "name": rule_label(r), "type": "calendar",
                             "alert": "Active" if first_active is not None else "Not active",
                             "first_alert": first_active, "highest_level": "-", "first_at_highest": None,
                             "days_Low": None, "days_Moderate": None, "days_High": None})
    summary = pd.DataFrame(summary_rows)

    out_path = out_dir / f"{event_id}_daily_{start_year}_{end_year}.xlsx"
    daily_sheet = f"{event_id}_daily"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        df.to_excel(writer, sheet_name=daily_sheet, startrow=1)  # row 1 left blank for the merged group header

    import openpyxl
    from openpyxl.styles import PatternFill, Font
    from openpyxl.utils import get_column_letter
    wb = openpyxl.load_workbook(out_path)

    ws = wb["summary"]
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for col_idx, name in enumerate(summary.columns, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(10, min(28, len(name) + 4))
    ws.column_dimensions["B"].width = 28  # "name" column: full rule label, needs more room
    ws.freeze_panes = "A2"

    ws = wb[daily_sheet]
    ws.column_dimensions["A"].width = 12  # date
    for col_idx, name in enumerate(df.columns, start=2):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(9, min(20, len(name) - 4))
    col = 2  # column A is the date index
    for label, col_names in groups:
        span = len(col_names)
        ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + span - 1)
        cell = ws.cell(row=1, column=col, value=label)
        cell.font = Font(bold=True)
        col += span
    for cell in ws[2]:
        cell.font = Font(bold=True)

    # Highlight the reported incident window's rows (yellow, matching how the source
    # workbook itself highlights a row) so it's findable inside four years of daily data.
    fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    inc_start, inc_end = pd.Timestamp(ev["start"]), pd.Timestamp(ev["end"])
    for row_idx, day in enumerate(df.index, start=3):  # rows 1-2 are the group/column headers
        if inc_start <= day <= inc_end:
            for col_idx in range(1, df.shape[1] + 2):
                ws.cell(row=row_idx, column=col_idx).fill = fill
    ws.freeze_panes = "B3"  # keep the date column visible while scrolling through the variables
    wb.save(out_path)
    print(f"Wrote {out_path} (incident window {inc_start:%d %b %Y} - {inc_end:%d %b %Y} highlighted; "
         "summary + daily sheets)")


# =============================================================================
# Unattended run of every event
# =============================================================================

def run_all(resume: bool = False) -> None:
    """Every event in sheet order: download what it needs, evaluate, write outputs.
    An event that fails (e.g. a CDS outage) is retried once after the others;
    the summary in backtest/ALL_EVENTS_SUMMARY.csv records what happened to each.
    With resume=True, events already marked done in that summary are skipped, so
    a run stopped with Ctrl+C carries on where it left off (downloaded months are
    never fetched twice either way)."""
    import traceback
    from datetime import datetime

    logger = pl.get_logger("all_events", LOG_DIR)
    events, _ = read_workbook()
    status: dict[str, dict] = {}
    summary_file = BT_DIR / "ALL_EVENTS_SUMMARY.csv"
    if resume and summary_file.exists():
        prev = pd.read_csv(summary_file)
        for _, r in prev[prev["status"] == "done"].iterrows():
            status[r["event"]] = {"event": r["event"], "S": r["S_alert"], "T": r["T_first_alert"],
                                  "U": r["U_highest"], "X": r["X_remarks"], "status": "done",
                                  "finished": r["finished"]}
        logger.info("Resuming: %d event(s) already done, skipped: %s", len(status), ", ".join(status))

    def attempt(eid: str) -> None:
        logger.info("=== %s start", eid)
        try:
            res = evaluate(eid)
            status[eid] = {**res, "status": "done", "finished": datetime.now().strftime("%Y-%m-%d %H:%M")}
            logger.info("=== %s done: S=%s T=%s U=%s", eid, res["S"], res["T"], res["U"])
        except Exception as e:  # noqa: BLE001 -- keep going through the night
            status[eid] = {"event": eid, "status": f"failed: {e!r}"[:300],
                           "finished": datetime.now().strftime("%Y-%m-%d %H:%M")}
            logger.error("=== %s FAILED\n%s", eid, traceback.format_exc())
        write_summary()

    def write_summary() -> None:
        rows = []
        for eid in events.index:
            ev = events.loc[eid]
            st = status.get(eid, {"status": "not run yet"})
            rows.append({"event": eid, "stress": ev["Pest / disease / stress"], "start": ev["start"],
                         "end": ev["end"], "cell": ev["cell"], "rules": ", ".join(ev["rules"]),
                         "S_alert": st.get("S"), "T_first_alert": st.get("T"), "U_highest": st.get("U"),
                         "status": st["status"], "finished": st.get("finished"), "X_remarks": st.get("X")})
        pd.DataFrame(rows).to_csv(summary_file, index=False)

    for eid in events.index:
        if status.get(eid, {}).get("status") == "done":
            continue
        attempt(eid)
    for eid in [e for e, st in status.items() if st["status"].startswith("failed")]:
        logger.info("Retrying %s", eid)
        attempt(eid)
    done = sum(st["status"] == "done" for st in status.values())
    logger.info("ALL EVENTS FINISHED: %d of %d done; see ALL_EVENTS_SUMMARY.csv", done, len(events))


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("all", help="Download and run every event in sheet order; one failure never stops the rest") \
        .add_argument("--resume", action="store_true", help="Skip events already marked done in ALL_EVENTS_SUMMARY.csv")
    for name in ("download", "run", "sources"):
        sub.add_parser(name).add_argument("event", help="Incident ID from sheet 1, e.g. I01")
    sp = sub.add_parser("spatial", help="Run one event's rules across every grid cell in Pandhurna, "
                        "continuously over full calendar years (not tied to that event's own incident window)")
    sp.add_argument("event", help="Incident ID whose rules to run, e.g. I01")
    sp.add_argument("start_year", type=int)
    sp.add_argument("end_year", type=int)
    sp.add_argument("--lead-months", type=int, default=6,
                    help="Months of real data before start_year, for the waterlogging water-balance to settle (default 6)")
    args = p.parse_args()

    if args.cmd == "list":
        events, _ = read_workbook()
        print(events[["Pest / disease / stress", "start", "end", "cell", "rules"]].to_string())
        return
    if args.cmd == "all":
        run_all(resume=args.resume)
        return
    if args.cmd == "spatial":
        spatial_multiyear(args.event.upper(), args.start_year, args.end_year, lead_months=args.lead_months)
        return
    event_id = args.event.upper()
    if args.cmd == "download":
        events, _ = read_workbook()
        ev = events.loc[event_id]
        months = event_months(ev)
        variables = rule_vars(ev["rules"])
        print(f"{event_id}: {len(variables)} variables x {len(months)} months "
              f"({months[0][0]}-{months[0][1]:02d} to {months[-1][0]}-{months[-1][1]:02d}): {', '.join(variables)}")
        download(variables, months)
        print("Download complete.")
    elif args.cmd == "sources":
        # The rules-and-sources note needs no weather data, so it can be written before the download.
        events, thresholds = read_workbook()
        ev = events.loc[event_id]
        out_dir = EVENTS_DIR / event_id
        out_dir.mkdir(parents=True, exist_ok=True)
        tested = [r for r in ev["rules"] if r in _registry() or r in CALENDAR_RULES]
        deferred = [r for r in ev["rules"] if r in DEFERRED_RULES]
        missing = [r for r in ev["rules"] if r not in tested and r not in deferred]
        write_sources(ev, event_id, thresholds, tested, deferred, missing, out_dir)
        print(f"Wrote {out_dir / f'{event_id}_rules_and_sources.md'}")
    else:
        evaluate(event_id)


if __name__ == "__main__":
    main()
