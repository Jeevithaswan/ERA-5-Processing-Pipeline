"""ERA5-Land download and daily-aggregation utilities for the climatology pipeline.

Everything here is place- and crop-agnostic: fetching hourly ERA5-Land data from
the Copernicus Climate Data Store, de-accumulating cumulative fields (rainfall,
solar radiation), aggregating to Indian-calendar daily values, and computing
FAO-56 Penman-Monteith reference evapotranspiration. Used by indices.py and
run_climatology.py for the drought/climate index pipeline.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import pipeline as pl  # noqa: E402

RAW_DIR = ROOT / "data" / "raw"
LOG_DIR = ROOT / "data" / "logs"

DATASET = "reanalysis-era5-land"
IST = pd.Timedelta(hours=5, minutes=30)

CDS_NAMES = {
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "tp": "total_precipitation",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "ssrd": "surface_solar_radiation_downwards",
    "sp": "surface_pressure",
}

# Months bundled into one CDS request. Almost all of a request's time is spent
# queued server-side, so fewer, larger requests finish sooner; a bundle CDS
# refuses falls back to single months (see _fetch).
MONTHS_PER_REQUEST = 3

_DEFAULTS = pl.load_defaults()


def months_between(first: date, last: date) -> list[tuple[int, int]]:
    out, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _raw_file(short: str, y: int, m: int, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / short / f"{short}_{y}_{m:02d}.nc"


def _fetch(y: int, months: list[int], shorts: list[str], logger,
           raw_dir: Path = RAW_DIR, area: list[float] = None, retries_left: int = 4) -> tuple[str, bool]:
    """One CDS request for some variables over one or more months of one year,
    split into one file per variable per month."""
    import cdsapi

    label = f"{y}-{'/'.join(f'{m:02d}' for m in months)} [{', '.join(shorts)}]"
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
            "area": area,
        }
        cdsapi.Client(quiet=True, progress=False).retrieve(DATASET, req, str(target))
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
            # would make this worse, so this waits and resubmits the SAME request
            # unchanged, backing off further each time it recurs.
            if retries_left <= 0:
                logger.error("Giving up on %s: still rate-limited after all retries", label)
                return label, False
            wait_s = 60 * (5 - retries_left)  # 60, 120, 180, 240s
            logger.info("Rate-limited on %s -- waiting %ds before resubmitting unchanged", label, wait_s)
            time.sleep(wait_s)
            return _fetch(y, months, shorts, logger, raw_dir, area, retries_left - 1)
        if len(months) > 1:
            # CDS may refuse a multi-month request as too large: fall back to one month at a time.
            logger.info("Retrying %s one month at a time", label)
            results = []
            for m in months:
                time.sleep(2)
                results.append(_fetch(y, [m], shorts, logger, raw_dir, area, retries_left))
            return label, all(ok for _, ok in results)
        if len(shorts) > 1:
            # Still too large even at one month: split the variable list in half and retry each half.
            mid = len(shorts) // 2
            logger.info("Retrying %s split into %s + %s", label, shorts[:mid], shorts[mid:])
            r1 = _fetch(y, months, shorts[:mid], logger, raw_dir, area, retries_left)
            time.sleep(2)
            r2 = _fetch(y, months, shorts[mid:], logger, raw_dir, area, retries_left)
            return label, r1[1] and r2[1]
        return label, False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def download(variables: list[str], months: list[tuple[int, int]],
             raw_dir: Path = RAW_DIR, area: list[float] = None, parallel: int | None = None) -> None:
    logger = pl.get_logger("download", LOG_DIR)
    raw_dir.mkdir(parents=True, exist_ok=True)
    total = len(variables) * len(months)
    groups: list[tuple[int, list[int], list[str]]] = []
    for y, m in months:
        need = [s for s in variables if not _raw_file(s, y, m, raw_dir).exists()]
        if not need:
            continue
        g = groups[-1] if groups else None
        if g and g[0] == y and g[2] == need and m == g[1][-1] + 1 and len(g[1]) < MONTHS_PER_REQUEST:
            g[1].append(m)
        else:
            groups.append((y, [m], need))
    if not groups:
        logger.info("All %d month-files already downloaded", total)
        return
    todo = sum(len(ms) * len(need) for _, ms, need in groups)
    logger.info("Need %d of %d month-files (%d already present) -> %d request(s)",
                todo, total, total - todo, len(groups))
    with ThreadPoolExecutor(max_workers=parallel or _DEFAULTS.get("cds_parallel", 4)) as ex:
        results = list(ex.map(lambda g: _fetch(g[0], g[1], g[2], logger, raw_dir, area), groups))
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


def _deaccumulated(short: str, months, raw_dir: Path = RAW_DIR) -> xr.DataArray:
    """Hourly amounts from an ERA5-Land accumulated field (tp, ssrd).

    ERA5-Land accumulates from 00 UTC: the 01 UTC value is the first hour's
    amount and each later value (through 00 UTC next day) is the running total.
    The running total is first made non-decreasing within each accumulation
    cycle (running maximum) before differencing, since raw accumulated values
    can have tiny rounding dips in flat night-time plateaus that a naive diff
    would otherwise read as a false reset."""
    da = load_hourly(short, months, raw_dir)
    cycle = (da["time"].dt.hour == 1).cumsum("time")
    axis = da.get_axis_num("time")
    da = da.groupby(cycle).map(lambda g: g.copy(data=np.maximum.accumulate(g.values, axis=axis)))
    diff = da.diff("time")
    first_hour = diff["time"].dt.hour == 1
    hourly = xr.where(first_hour, da.isel(time=slice(1, None)), diff)
    return hourly.transpose(*da.dims)


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


def daily_fields(variables: list[str], months: list[tuple[int, int]], raw_dir: Path = RAW_DIR) -> xr.Dataset:
    """Daily fields on Indian (IST) calendar days -- the incident dates are Indian
    dates, and a UTC day would split the Indian night across two days. Days with
    fewer than 24 hourly temperature values (the edges of the download) are dropped.

    Always includes daily tmax/tmin/tmean. Adds the FAO-56 Penman-Monteith inputs
    (tdew, u10, rain_mm, rs_mj, p_kpa) when their source variables are requested,
    and computes et0_mm once all five are present."""
    def daily(h, how):
        h = h.assign_coords(time=h["time"] + IST)
        return getattr(h.resample(time="1D"), how)()

    out = {}
    t = load_hourly("t2m", months, raw_dir) - 273.15
    full = daily(t, "count") == 24
    out["tmax"], out["tmin"], out["tmean"] = daily(t, "max"), daily(t, "min"), daily(t, "mean")
    if "d2m" in variables:
        td = load_hourly("d2m", months, raw_dir) - 273.15
        out["tdew"] = daily(td, "mean")
    if "tp" in variables:
        out["rain_mm"] = daily(_deaccumulated("tp", months, raw_dir) * 1000.0, "sum")
    if "u10" in variables:
        ws = np.hypot(load_hourly("u10", months, raw_dir), load_hourly("v10", months, raw_dir))
        out["u10"] = daily(ws, "mean")
    if "ssrd" in variables:
        out["rs_mj"] = daily(_deaccumulated("ssrd", months, raw_dir) / 1e6, "sum")
    if "sp" in variables:
        out["p_kpa"] = daily(load_hourly("sp", months, raw_dir) / 1000.0, "mean")

    ds = xr.Dataset(out).where(full)
    ds = ds.sel(time=full.any(["latitude", "longitude"]))
    if {"rain_mm", "rs_mj", "p_kpa", "tdew", "u10"} <= set(ds.data_vars):
        ds["et0_mm"] = fao56_et0(ds)
    return ds
