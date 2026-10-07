"""
ERA5 processing pipeline -- single-file version.

Downloads gridded ERA5-Land reanalysis data from the Copernicus Climate Data
Store (CDS), preprocesses it to daily NetCDF, computes agrometeorological
indicators, climatic normals, anomalies, climate indices, and PNG charts.
Location, time period and variables are runtime inputs, not hardcoded.

HOW TO RUN
----------
No arguments -- interactive menu, answer questions one at a time:
    python pipeline.py

With flags -- for scripting / repeat runs:
    python pipeline.py download   --boundary Pandhurna.kml --start 2023 --end 2023
    python pipeline.py preprocess --boundary Pandhurna.kml --start 2023 --end 2023
    python pipeline.py agromet    --boundary Pandhurna.kml --start 2023 --end 2023
    python pipeline.py normals    --boundary Pandhurna.kml --normals-start 2023 --normals-end 2023
    python pipeline.py anomalies  --boundary Pandhurna.kml --start 2023 --end 2023 --normals-start 2023 --normals-end 2023
    python pipeline.py indices    --boundary Pandhurna.kml --start 2023 --end 2023 --normals-start 2023 --normals-end 2023
    python pipeline.py plots      --boundary Pandhurna.kml --start 2023 --end 2023
    python pipeline.py all        --boundary Pandhurna.kml --start 2023 --end 2023 --normals-start 2023 --normals-end 2023

A plain bounding box instead of a boundary file (no file needed):
    python pipeline.py download --bbox 21.3 78.9 20.9 79.3 --region-name nagpur --start 2024 --end 2024 --variables t2m,tp

Settings that rarely change (variable list, aggregation rules, normals baseline,
indices list, thresholds) live in config/defaults.yaml, not in this file.
"""
from __future__ import annotations

import argparse
import calendar
import getpass
import os
import platform
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
import yaml
from matplotlib.colors import LinearSegmentedColormap

PIPELINE_VERSION = "0.2.0"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULTS_PATH = PROJECT_ROOT / "config" / "defaults.yaml"


# =============================================================================
# Shared helpers: defaults loading, logging, de-accumulation, path management
# =============================================================================

def load_defaults(path: str | Path = DEFAULTS_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_logger(name: str, log_dir: Path | None = None):
    import logging

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def cds_long_name(defaults: dict[str, Any], short_name: str) -> str:
    for long_name, short in defaults["variables"].items():
        if short == short_name:
            return long_name
    raise KeyError(
        f"Unknown variable '{short_name}'. Known short names: {list(defaults['variables'].values())}"
    )


def load_daily(processed_dir: Path, short: str, year: int) -> xr.Dataset | None:
    f = processed_dir / short / f"{short}_{year}_daily.nc"
    if not f.exists():
        return None
    return xr.open_dataset(f)


def deaccumulate(da: xr.DataArray) -> xr.DataArray:
    """Reconstruct true per-timestep deltas from an ERA5/ERA5-Land accumulated
    field (e.g. total_precipitation), which resets to a new cumulative count
    once per day rather than each raw hourly value already being a 1-hour
    amount. A reset is detected wherever the cumulative value drops instead
    of rising; that timestep's raw value is then the amount since reset,
    not a diff against the previous (higher, prior-cycle) reading. Verified
    against real ERA5-Land data: resets between 00:00 and 01:00 UTC daily,
    confirmed by the fact that summing the reconstructed deltas across a
    calendar day exactly reproduces the next day's 00:00 cumulative value.
    """
    diff = da.diff(dim="time")  # diff[t] = da[t] - da[t-1], labeled at da.time[1:]
    reset = diff < 0
    delta = xr.where(reset, da.isel(time=slice(1, None)), diff)
    first = da.isel(time=slice(0, 1))  # no prior sample to diff against; best-effort as-is
    return xr.concat([first, delta], dim="time").transpose(*da.dims)


def load_baseline(processed_dir: Path, short: str, start_year: int, end_year: int) -> xr.Dataset:
    var_dir = processed_dir / short
    files = [var_dir / f"{short}_{y}_daily.nc" for y in range(start_year, end_year + 1)]
    files = [f for f in files if f.exists()]
    if not files:
        raise FileNotFoundError(
            f"No processed daily files found for '{short}' in {start_year}-{end_year} under {var_dir}. "
            "Run the preprocess stage for the normals baseline period first."
        )
    return xr.open_mfdataset([str(f) for f in files], combine="by_coords")


# =============================================================================
# Region: resolve a boundary file or bounding box into a CDS download area
# =============================================================================

def _unblock_pyproj_env() -> None:
    """Some machines set a system-wide PROJ_LIB pointing at a non-Python PROJ
    install (e.g. PostGIS's bundled copy), which shadows pyproj/rioxarray's own
    and breaks CRS lookups. Clearing it for this process only (not the user's
    persistent environment) makes pyproj fall back to its own bundled database.
    """
    for var in ("PROJ_LIB", "PROJ_DATA"):
        value = os.environ.get(var, "")
        if value and "site-packages" not in value.replace("\\", "/"):
            os.environ.pop(var, None)


@dataclass
class Region:
    name: str
    area: list[float]  # [North, West, South, East]
    polygon: Any | None = None  # shapely geometry in EPSG:4326, or None for a plain bbox


def resolve_region(
    bbox: tuple[float, float, float, float] | None,
    boundary_file: str | Path | None,
    name: str | None,
    buffer_deg: float,
) -> Region:
    if bool(bbox) == bool(boundary_file):
        raise ValueError("Provide exactly one of --bbox or --boundary")

    if boundary_file is not None:
        _unblock_pyproj_env()
        import geopandas as gpd

        boundary_file = Path(boundary_file)
        gdf = gpd.read_file(boundary_file).to_crs(4326)
        polygon = gdf.geometry.union_all()
        minx, miny, maxx, maxy = polygon.bounds
        area = [
            maxy + buffer_deg,  # North
            minx - buffer_deg,  # West
            miny - buffer_deg,  # South
            maxx + buffer_deg,  # East
        ]
        return Region(name=name or boundary_file.stem, area=area, polygon=polygon)

    # bbox given as (north, west, south, east)
    north, west, south, east = bbox
    return Region(name=name or "custom_region", area=[north, west, south, east], polygon=None)


def clip_to_polygon(ds: xr.Dataset, region: Region) -> xr.Dataset:
    """Mask grid cells outside `region.polygon` to NaN. No-op if region has no polygon."""
    if region.polygon is None:
        return ds

    _unblock_pyproj_env()
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    from shapely.geometry import mapping

    # Integer dtypes have no NaN to mask with, so rioxarray fills clipped-out cells
    # with a dtype-min sentinel (e.g. -9223372036854775808 for int64) instead --
    # cast to float first so masked cells are genuinely NaN like every float field.
    # Boolean dtypes have the same problem and were missed by this check: confirmed
    # empirically that clipped-out cells on a bool array come back as True instead
    # of NaN, silently inflating any rule (e.g. X-W01) evaluated outside the real
    # boundary -- np.issubdtype(bool, np.integer) is False, so bool needs its own
    # branch here rather than being covered by the integer check above it.
    for name, da in ds.data_vars.items():
        if np.issubdtype(da.dtype, np.integer) or da.dtype == bool:
            ds[name] = da.astype("float64")

    x_dim = "longitude" if "longitude" in ds.dims else "lon"
    y_dim = "latitude" if "latitude" in ds.dims else "lat"

    ds = ds.rio.set_spatial_dims(x_dim=x_dim, y_dim=y_dim)
    ds = ds.rio.write_crs("EPSG:4326")
    return ds.rio.clip([mapping(region.polygon)], "EPSG:4326", drop=False, all_touched=True)


# =============================================================================
# Metadata: CF-1.8-style attributes attached to every NetCDF this pipeline writes
# =============================================================================

VAR_ATTRS: dict[str, dict[str, str]] = {
    "t2m_mean": {"standard_name": "air_temperature", "long_name": "Daily mean 2 m air temperature", "units": "degC"},
    "t2m_max": {"standard_name": "air_temperature", "long_name": "Daily maximum 2 m air temperature", "units": "degC"},
    "t2m_min": {"standard_name": "air_temperature", "long_name": "Daily minimum 2 m air temperature", "units": "degC"},
    "d2m_mean": {"standard_name": "dew_point_temperature", "long_name": "Daily mean 2 m dewpoint temperature", "units": "degC"},
    "tp_sum": {"standard_name": "precipitation_amount", "long_name": "Daily total precipitation", "units": "mm",
               "comment": "De-accumulated from ERA5-Land's daily-resetting cumulative total_precipitation field."},
    "u10_mean": {"standard_name": "eastward_wind", "long_name": "Daily mean 10 m eastward wind component", "units": "m s-1"},
    "v10_mean": {"standard_name": "northward_wind", "long_name": "Daily mean 10 m northward wind component", "units": "m s-1"},
    "sp_mean": {"standard_name": "surface_air_pressure", "long_name": "Daily mean surface pressure", "units": "hPa"},
    "rh_mean": {"long_name": "Daily mean relative humidity", "units": "%",
                "comment": "Alduchov & Eskridge (1996) Magnus-form approximation, J. Applied Meteorology 35:601-609."},
    "rh_max": {"long_name": "Daily maximum relative humidity", "units": "%"},
    "rh_min": {"long_name": "Daily minimum relative humidity", "units": "%"},
    "wind_mean_ms": {"long_name": "Daily mean 10 m wind speed", "units": "m s-1"},
    "wind_max_ms": {"long_name": "Daily maximum 10 m wind speed", "units": "m s-1"},
    "rain_mm": {"long_name": "Daily total precipitation (agromet stage)", "units": "mm"},
    "rain_day": {"long_name": "Day meeting the IMD rainy-day threshold", "units": "1",
                 "comment": "1 if daily precipitation >= rain_day_mm (config), else 0."},
    "leaf_wetness_hours": {"long_name": "Hours with relative humidity above the leaf-wetness proxy threshold",
                            "units": "h", "comment": "Sentelhas et al. (2008), Int. J. Biometeorology 52:565-575."},
    "wind_driven_rain_hours": {"long_name": "Hours meeting the wind-driven-rain disease-trigger condition",
                                "units": "h", "comment": "Measurable rain (WMO) concurrent with wind above threshold (config)."},
    "rain_days": {"long_name": "Annual count of rainy days (IMD threshold)", "units": "1"},
    "wet_spell_max": {"long_name": "Longest run of consecutive rainy days in the calendar year", "units": "d"},
    "dry_spell_max": {"long_name": "Longest run of consecutive non-rainy days in the calendar year", "units": "d"},
}


def _history_line(extra: str = "") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    who = f"{getpass.getuser()}@{platform.node()}"
    line = f"{ts} era5_pipeline v{PIPELINE_VERSION} ({who})"
    return f"{line}: {extra}" if extra else line


def stamp(
    ds: xr.Dataset,
    defaults: dict[str, Any],
    region_name: str,
    title: str,
    extra_history: str = "",
) -> xr.Dataset:
    """Attach CF-style global + per-variable attributes to `ds` in place, and return it."""
    ds.attrs.update(
        {
            "Conventions": "CF-1.8",
            "title": title,
            "institution": ds.attrs.get("institution", "(institution not set)"),
            "source": f"ECMWF {defaults.get('dataset', 'reanalysis-era5-land')} reanalysis, "
                      "via Copernicus Climate Data Store (https://cds.climate.copernicus.eu)",
            "region": region_name,
            "references": "Hersbach et al. (2020), Q. J. R. Meteorol. Soc. 146:1999-2049 (ERA5); "
                          "WMO-No. 1203 (2017) for climatic-normals methodology.",
            "history": _history_line(extra_history),
        }
    )
    for name, da in ds.data_vars.items():
        # Raw CDS/GRIB decoding leaves stale or placeholder metadata (e.g.
        # standard_name='unknown', long_name from the un-aggregated source
        # variable) that survives xarray operations via keep_attrs -- our
        # own definitions are authoritative for the keys they cover, so they
        # overwrite rather than only fill gaps. Everything GRIB_*-prefixed is
        # decoder bookkeeping, not accurate for a derived daily/annual product.
        da.attrs = {k: v for k, v in da.attrs.items() if not k.startswith("GRIB_")}
        base = name.split("_anomaly")[0]
        attrs = VAR_ATTRS.get(base) or VAR_ATTRS.get(name)
        if attrs:
            da.attrs.update(attrs)
        if name.endswith("_anomaly"):
            da.attrs["long_name"] = f"Anomaly ({da.attrs.get('long_name', base)} minus day-of-year normal)"
    return ds


# =============================================================================
# Stage 1: download gridded ERA5/ERA5-Land hourly data from the Copernicus CDS
# =============================================================================
# Requires a CDS API key in ~/.cdsapirc (see README.md).
# Requests are submitted in parallel (default 4 at once, config: cds_parallel) --
# each request spends ~1-2 minutes queued on CDS's side before it even starts
# running, almost all wait rather than transfer, so submitting several at once
# cuts wall-clock time roughly by the parallelism factor.

ALL_HOURS = [f"{h:02d}:00" for h in range(24)]


def _unwrap_if_zipped(out_file: Path) -> None:
    """CDS sometimes returns a zip containing one .nc file even when
    format='netcdf' was requested (observed on reanalysis-era5-land as of
    2026). Detect this by content, not extension, and replace out_file with
    the actual NetCDF inside so downstream stages can open it directly.
    """
    if not zipfile.is_zipfile(out_file):
        return
    with zipfile.ZipFile(out_file) as zf:
        members = zf.namelist()
        if len(members) != 1:
            raise ValueError(f"Expected exactly one file inside {out_file}, found {members}")
        data = zf.read(members[0])
    out_file.write_bytes(data)


def build_request(long_var_name: str, area: list[float], year: int, month: int) -> dict[str, Any]:
    n_days = calendar.monthrange(year, month)[1]
    return {
        "product_type": "reanalysis",
        "format": "netcdf",
        "variable": long_var_name,
        "year": str(year),
        "month": f"{month:02d}",
        "day": [f"{d:02d}" for d in range(1, n_days + 1)],
        "time": ALL_HOURS,
        "area": area,
    }


def _fetch_one(dataset: str, request: dict[str, Any], out_file: Path, label: str, logger) -> tuple[str, bool]:
    import cdsapi

    logger.info("Requesting %s", label)
    try:
        # A fresh client per thread avoids relying on cdsapi.Client (which wraps a
        # requests.Session) being safe to share across concurrent calls.
        cdsapi.Client(quiet=True, progress=False).retrieve(dataset, request, str(out_file))
        _unwrap_if_zipped(out_file)
        logger.info("Done %s", label)
        return label, True
    except Exception:
        logger.exception("Failed to download %s", label)
        # Leave any partial file for manual inspection rather than deleting it --
        # resume logic relies on completeness, so a partial file should be
        # checked, not treated as done.
        return label, False


def download_all(
    defaults: dict[str, Any],
    region: Region,
    variables: list[str],
    start_year: int,
    end_year: int,
    raw_dir: Path,
    dataset: str | None = None,
    log_dir: Path | None = None,
    parallel: int | None = None,
) -> None:
    logger = get_logger("download", log_dir)
    dataset = dataset or defaults["dataset"]
    parallel = parallel or defaults.get("cds_parallel", 4)

    jobs = []
    for short in variables:
        long_name = cds_long_name(defaults, short)
        var_dir = raw_dir / short
        var_dir.mkdir(parents=True, exist_ok=True)

        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                out_file = var_dir / f"{short}_{year}_{month:02d}.nc"
                if out_file.exists():
                    logger.info("Skipping existing file: %s", out_file)
                    continue
                request = build_request(long_name, region.area, year, month)
                label = f"{short} {year}-{month:02d} (region '{region.name}')"
                jobs.append((request, out_file, label))

    if not jobs:
        logger.info("Nothing to download, all files already exist")
        return

    logger.info("Submitting %d download(s), %d in parallel", len(jobs), parallel)
    failures = []
    from tqdm import tqdm

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = [ex.submit(_fetch_one, dataset, req, out_file, label, logger) for req, out_file, label in jobs]
        bar = tqdm(as_completed(futures), total=len(futures), desc="Downloading", unit="file")
        for f in bar:
            label, ok = f.result()
            if not ok:
                failures.append(label)
        bar.close()

    if failures:
        raise RuntimeError(f"{len(failures)}/{len(jobs)} downloads failed: {failures}")


# =============================================================================
# Stage 2: turn raw hourly ERA5 NetCDF files into clean daily NetCDFs
# =============================================================================
# total_precipitation is handled separately from the other (instantaneous) variables:
# ERA5/ERA5-Land delivers it as a cumulative total that resets once per day (confirmed
# against real data -- see deaccumulate() above), not as independent 1-hour amounts, so it
# needs de-accumulating before a daily sum means anything. That requires the full year of
# hourly data at once (a day near a month boundary needs the next month's first reading),
# unlike the other variables which are aggregated per month independently.

def _convert_units(da: xr.DataArray, short: str) -> xr.DataArray:
    if short in ("t2m", "d2m"):
        da = da - 273.15
        da.attrs["units"] = "degC"
    elif short == "sp":
        da = da / 100.0
        da.attrs["units"] = "hPa"
    elif short == "tp":
        da = da * 1000.0
        da.attrs["units"] = "mm"
    return da


def _load_month(var_dir: Path, short: str, year: int, month: int) -> xr.Dataset | None:
    f = var_dir / f"{short}_{year}_{month:02d}.nc"
    if not f.exists():
        return None
    ds = xr.open_dataset(f)
    if "valid_time" in ds.dims:
        ds = ds.rename(valid_time="time")
    return ds


def aggregate_to_daily(ds: xr.Dataset, short: str, agg_list: list[str]) -> xr.Dataset:
    da = _convert_units(ds[short], short)
    daily_vars = {}
    for agg in agg_list:
        resampled = da.resample(time="1D")
        method = getattr(resampled, agg)
        daily_vars[f"{short}_{agg}"] = method()
    return xr.Dataset(daily_vars)


def _process_precip_year(raw_dir: Path, year: int, log_dir: Path | None) -> xr.Dataset | None:
    logger = get_logger("preprocess", log_dir)
    var_dir = raw_dir / "tp"
    files = [var_dir / f"tp_{year}_{m:02d}.nc" for m in range(1, 13)]
    files = [f for f in files if f.exists()]
    if not files:
        logger.warning("No data found for tp %04d, nothing to write", year)
        return None
    if len(files) < 12:
        logger.warning("Only %d/12 months found for tp %04d -- de-accumulation across the "
                        "missing month boundary will be approximate there", len(files), year)

    ds = xr.open_mfdataset([str(f) for f in files], combine="by_coords")
    if "valid_time" in ds.dims:
        ds = ds.rename(valid_time="time")
    ds = ds.load()

    hourly_mm = deaccumulate(ds["tp"]).clip(min=0) * 1000.0
    daily = hourly_mm.resample(time="1D").sum()
    daily.name = "tp_sum"
    daily.attrs["units"] = "mm"

    # The last calendar day of the requested range needs the following day's 00:00
    # reading (not downloaded) to close out its accumulation cycle; that day's total
    # is an undercount using only the hours actually available. Flag it rather than
    # silently returning a wrong-looking number.
    last_day = daily["time"].max()
    logger.warning("Last day in tp %04d (%s) is an undercount -- missing the reading "
                    "just after midnight the following day", year, str(last_day.values)[:10])

    return daily.to_dataset()


def process_variable_year(
    defaults: dict[str, Any],
    short: str,
    year: int,
    raw_dir: Path,
    processed_dir: Path,
    region: Region,
    log_dir: Path | None = None,
) -> Path | None:
    logger = get_logger("preprocess", log_dir)
    out_dir = processed_dir / short
    out_dir.mkdir(parents=True, exist_ok=True)

    if short == "tp":
        year_ds = _process_precip_year(raw_dir, year, log_dir)
        if year_ds is None:
            return None
    else:
        var_dir = raw_dir / short
        agg_list = defaults["aggregation"][short]

        monthly_daily = []
        for month in range(1, 13):
            ds = _load_month(var_dir, short, year, month)
            if ds is None:
                logger.warning("Missing raw file for %s %04d-%02d, skipping", short, year, month)
                continue
            monthly_daily.append(aggregate_to_daily(ds, short, agg_list))
            ds.close()

        if not monthly_daily:
            logger.warning("No data found for %s %04d, nothing to write", short, year)
            return None

        year_ds = xr.concat(monthly_daily, dim="time").sortby("time")

    year_ds = clip_to_polygon(year_ds, region)
    year_ds = stamp(year_ds, defaults, region.name, f"ERA5-Land daily {short} -- {region.name} {year}",
                     extra_history=f"preprocess stage, variable={short}, year={year}")

    out_file = out_dir / f"{short}_{year}_daily.nc"
    year_ds.to_netcdf(out_file)
    logger.info("Wrote %s", out_file)
    return out_file


def preprocess_all(
    defaults: dict[str, Any],
    variables: list[str],
    start_year: int,
    end_year: int,
    raw_dir: Path,
    processed_dir: Path,
    region: Region,
    log_dir: Path | None = None,
) -> None:
    for short in variables:
        for year in range(start_year, end_year + 1):
            process_variable_year(defaults, short, year, raw_dir, processed_dir, region, log_dir)


# =============================================================================
# Stage 2b: daily agrometeorological indicators (grid version)
# =============================================================================
# Generalized from the senior team's point-based pandhurna_daily_warning_system
# pipeline (era5.py / indicators.py) to a full grid rather than discrete points.
# Unlike preprocess, which aggregates each variable independently, these
# indicators need t2m, d2m, tp, u10 and v10 merged at hourly resolution before
# aggregating to daily -- humidity and the wind-driven-rain trigger are each a
# function of two variables at the same hour, not of one variable alone.
#
# Formulas (all thresholds configurable in config/defaults.yaml):
#   Humidity  e(T) = 6.1094 * exp(17.625*T / (T+243.04)) hPa
#             (Alduchov & Eskridge 1996, J. Applied Meteorology 35:601-609)
#             RH = 100 * e(Td) / e(T), clipped to 100
#   Wind      |V| = sqrt(u10^2 + v10^2)
#   Rain day  daily total >= rain_day_mm (IMD convention)
#   Wind-driven rain hours
#             hours with rain >= measurable_rain_mm_h (WMO measurable) AND
#             wind > wind_driven_rain_wind_ms -- a common disease trigger
#             (e.g. citrus canker)
#   Leaf-wetness-duration proxy
#             hours with RH >= leaf_wetness_rh_pct, a published proxy where no
#             leaf-wetness sensor exists (Sentelhas et al. 2008,
#             Int. J. Biometeorology 52:565-575)

NON_PRECIP_VARS = ["t2m", "d2m", "u10", "v10"]


def _e_hpa(t_c: xr.DataArray) -> xr.DataArray:
    """Saturation vapour pressure (hPa) via the Alduchov & Eskridge (1996) Magnus-form approximation."""
    return 6.1094 * np.exp(17.625 * t_c / (t_c + 243.04))


def _load_month_merged(raw_dir: Path, hourly_rain_mm: xr.DataArray, year: int, month: int) -> xr.Dataset | None:
    das = {}
    for short in NON_PRECIP_VARS:
        f = raw_dir / short / f"{short}_{year}_{month:02d}.nc"
        if not f.exists():
            return None
        ds = xr.open_dataset(f)
        if "valid_time" in ds.dims:
            ds = ds.rename(valid_time="time")
        das[short] = ds[short]
    merged = xr.Dataset(das)
    merged["rain_mm"] = hourly_rain_mm.sel(time=merged["time"])
    return merged


def _hourly_rain_mm_for_year(raw_dir: Path, year: int, log_dir: Path | None) -> xr.DataArray | None:
    """True per-hour rain amounts (mm), de-accumulated across the whole year so
    month-boundary hours are reconstructed correctly -- see _process_precip_year
    for why total_precipitation can't just be used as-is.
    """
    var_dir = raw_dir / "tp"
    files = [var_dir / f"tp_{year}_{m:02d}.nc" for m in range(1, 13)]
    files = [f for f in files if f.exists()]
    if not files:
        return None

    ds = xr.open_mfdataset([str(f) for f in files], combine="by_coords")
    if "valid_time" in ds.dims:
        ds = ds.rename(valid_time="time")
    ds = ds.load()
    hourly_mm = (deaccumulate(ds["tp"]).clip(min=0) * 1000.0)
    hourly_mm.name = "rain_mm"
    return hourly_mm


def compute_agromet_year(
    defaults: dict[str, Any],
    year: int,
    raw_dir: Path,
    processed_dir: Path,
    region: Region,
    log_dir: Path | None = None,
) -> Path | None:
    logger = get_logger("agromet", log_dir)
    out_dir = processed_dir / "agromet"
    out_dir.mkdir(parents=True, exist_ok=True)

    rain_day_mm = defaults["rain_day_mm"]
    measurable_rain_mm_h = defaults["measurable_rain_mm_h"]
    leaf_wetness_rh_pct = defaults["leaf_wetness_rh_pct"]
    wind_thresh_ms = defaults["wind_driven_rain_wind_ms"]

    hourly_rain_mm = _hourly_rain_mm_for_year(raw_dir, year, log_dir)
    if hourly_rain_mm is None:
        logger.warning("No tp data found for agromet %04d, nothing to write", year)
        return None

    monthly_daily = []
    for month in range(1, 13):
        h = _load_month_merged(raw_dir, hourly_rain_mm, year, month)
        if h is None:
            logger.warning("Missing raw hourly data for agromet %04d-%02d, skipping", year, month)
            continue

        t_c = h["t2m"] - 273.15
        td_c = h["d2m"] - 273.15
        rh = (100 * _e_hpa(td_c) / _e_hpa(t_c)).clip(max=100)
        ws = np.hypot(h["u10"], h["v10"])
        rain_mm = h["rain_mm"]
        rain_wind_hr = ((rain_mm >= measurable_rain_mm_h) & (ws > wind_thresh_ms)).astype(int)
        wet_hr = (rh >= leaf_wetness_rh_pct).astype(int)

        daily = xr.Dataset(
            {
                "rh_mean": rh.resample(time="1D").mean(),
                "rh_max": rh.resample(time="1D").max(),
                "rh_min": rh.resample(time="1D").min(),
                "wind_mean_ms": ws.resample(time="1D").mean(),
                "wind_max_ms": ws.resample(time="1D").max(),
                "rain_mm": rain_mm.resample(time="1D").sum(),
                "leaf_wetness_hours": wet_hr.resample(time="1D").sum(),
                "wind_driven_rain_hours": rain_wind_hr.resample(time="1D").sum(),
            }
        )
        monthly_daily.append(daily)

    if not monthly_daily:
        logger.warning("No data found for agromet %04d, nothing to write", year)
        return None

    year_ds = xr.concat(monthly_daily, dim="time").sortby("time")
    year_ds["rain_day"] = (year_ds["rain_mm"] >= rain_day_mm).astype(int)
    year_ds = clip_to_polygon(year_ds, region)
    year_ds = stamp(year_ds, defaults, region.name, f"Daily agrometeorological indicators -- {region.name} {year}",
                     extra_history=f"agromet stage, year={year}")

    out_file = out_dir / f"agromet_{year}_daily.nc"
    year_ds.to_netcdf(out_file)
    logger.info("Wrote %s", out_file)
    return out_file


def compute_agromet_all(
    defaults: dict[str, Any],
    start_year: int,
    end_year: int,
    raw_dir: Path,
    processed_dir: Path,
    region: Region,
    log_dir: Path | None = None,
) -> None:
    for year in range(start_year, end_year + 1):
        compute_agromet_year(defaults, year, raw_dir, processed_dir, region, log_dir)


# =============================================================================
# Stage 3: compute WMO-standard daily climatic normals (default 1991-2020)
# =============================================================================
# For each daily variable (t2m_mean, tp_sum, ...), computes the mean value for
# each day-of-year across the baseline period, then smooths across day-of-year
# with a centered rolling window to reduce sampling noise -- standard WMO
# practice for daily normals.

def _smooth_circular(clim: xr.Dataset, window: int) -> xr.Dataset:
    """Centered rolling mean over day-of-year, wrapping around year end/start."""
    if window <= 1:
        return clim
    half = window // 2
    # Wrap-pad along dayofyear so the smoothing window is continuous across
    # Dec 31 -> Jan 1, rather than leaving edge days under-smoothed.
    padded = xr.concat(
        [clim.isel(dayofyear=slice(-half, None)), clim, clim.isel(dayofyear=slice(0, half))],
        dim="dayofyear",
    )
    smoothed = padded.rolling(dayofyear=window, center=True).mean()
    return smoothed.isel(dayofyear=slice(half, half + clim.sizes["dayofyear"])).assign_coords(
        dayofyear=clim["dayofyear"]
    )


def compute_normals(
    variables: list[str],
    processed_dir: Path,
    normals_dir: Path,
    normals_start_year: int,
    normals_end_year: int,
    smoothing_window_days: int = 5,
    log_dir: Path | None = None,
    defaults: dict[str, Any] | None = None,
    region_name: str = "",
) -> None:
    logger = get_logger("normals", log_dir)
    normals_dir.mkdir(parents=True, exist_ok=True)

    for short in variables:
        logger.info("Computing %d-%d normals for %s", normals_start_year, normals_end_year, short)
        ds = load_baseline(processed_dir, short, normals_start_year, normals_end_year)
        clim = ds.groupby("time.dayofyear").mean("time")
        clim = _smooth_circular(clim, smoothing_window_days)
        clim = clim.compute()
        if defaults is not None:
            clim = stamp(clim, defaults, region_name,
                         f"{normals_start_year}-{normals_end_year} climatic normal, {short} -- {region_name}",
                         extra_history=f"normals stage, variable={short}, baseline={normals_start_year}-{normals_end_year}")

        out_file = normals_dir / f"{short}_normals_{normals_start_year}_{normals_end_year}.nc"
        clim.to_netcdf(out_file)
        logger.info("Wrote %s", out_file)
        ds.close()


# =============================================================================
# Stage 4: compute daily anomalies relative to the climatic normals
# =============================================================================
# Anomaly = daily value - normal value for the same day-of-year.
# For precipitation (tp_sum), also computes a percent-of-normal anomaly, since
# precip anomalies are conventionally expressed relative to the climatological
# mean rather than as an absolute difference (a fixed mm offset is not
# meaningful across a wide dynamic range of daily rainfall).

def compute_anomalies_for_year(
    short: str,
    year: int,
    processed_dir: Path,
    normals_dir: Path,
    anomalies_dir: Path,
    normals_start_year: int,
    normals_end_year: int,
    log_dir: Path | None = None,
    defaults: dict[str, Any] | None = None,
    region_name: str = "",
) -> None:
    logger = get_logger("anomalies", log_dir)
    ds = load_daily(processed_dir, short, year)
    if ds is None:
        logger.warning("No processed data for %s %04d, skipping", short, year)
        return

    normals_file = normals_dir / f"{short}_normals_{normals_start_year}_{normals_end_year}.nc"
    normals = xr.open_dataset(normals_file)

    doy = ds["time"].dt.dayofyear
    matched_normals = normals.sel(dayofyear=doy)

    anomaly = ds - matched_normals.drop_vars("dayofyear")
    anomaly = anomaly.rename({v: f"{v}_anomaly" for v in anomaly.data_vars})

    if short == "tp":
        pct = (ds["tp_sum"] / matched_normals["tp_sum"].where(matched_normals["tp_sum"] > 0)) * 100.0
        anomaly["tp_sum_pct_of_normal"] = pct

    if defaults is not None:
        anomaly = stamp(anomaly, defaults, region_name,
                         f"Daily anomaly vs {normals_start_year}-{normals_end_year} normal, {short} -- {region_name} {year}",
                         extra_history=f"anomalies stage, variable={short}, year={year}")

    out_dir = anomalies_dir / short
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{short}_{year}_anomaly.nc"
    anomaly.to_netcdf(out_file)
    logger.info("Wrote %s", out_file)

    ds.close()
    normals.close()


def compute_all_anomalies(
    variables: list[str],
    start_year: int,
    end_year: int,
    processed_dir: Path,
    normals_dir: Path,
    anomalies_dir: Path,
    normals_start_year: int,
    normals_end_year: int,
    log_dir: Path | None = None,
    defaults: dict[str, Any] | None = None,
    region_name: str = "",
) -> None:
    for short in variables:
        for year in range(start_year, end_year + 1):
            compute_anomalies_for_year(
                short, year, processed_dir, normals_dir, anomalies_dir,
                normals_start_year, normals_end_year, log_dir, defaults, region_name,
            )


# =============================================================================
# Stage 5: compute annual indices -- generic ETCCDI/ECA&D climate indices (via
# xclim) and agromet/pest-warning indices generalized from the senior team's
# point-based pipeline
# =============================================================================
# Each entry in INDICATOR_REGISTRY is a function(processed_dir, year, baseline_start,
# baseline_end) -> xr.DataArray. Indices are computed one at a time and failures are
# logged rather than fatal, since xclim's exact function names/signatures can shift
# between versions -- check the logged error against your installed xclim version if
# an indicator fails.
#
# The agromet indices (rain_days, leaf_wetness_hours, wind_driven_rain_hours,
# wet_spell_max, dry_spell_max) read processed_dir/agromet/agromet_<year>_daily.nc,
# produced by the agromet stage above -- run that stage before requesting them.

def _var(processed_dir: Path, short: str, year: int, var_name: str, units: str) -> xr.DataArray:
    ds = load_daily(processed_dir, short, year)
    if ds is None:
        raise FileNotFoundError(f"Missing processed data for {short} {year} under {processed_dir}")
    da = ds[var_name]
    da.attrs["units"] = units
    return da


def _var_baseline(processed_dir: Path, short: str, start_year: int, end_year: int, var_name: str, units: str) -> xr.DataArray:
    ds = load_baseline(processed_dir, short, start_year, end_year)
    da = ds[var_name]
    da.attrs["units"] = units
    return da


def _tasmax(processed_dir, year):
    return _var(processed_dir, "t2m", year, "t2m_max", "degC")


def _tasmin(processed_dir, year):
    return _var(processed_dir, "t2m", year, "t2m_min", "degC")


def _pr(processed_dir, year):
    return _var(processed_dir, "tp", year, "tp_sum", "mm/day")


def _tx90p(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    from xclim.core.calendar import percentile_doy

    tasmax = _tasmax(processed_dir, year)
    baseline = _var_baseline(processed_dir, "t2m", start_year, end_year, "t2m_max", "degC")
    tasmax_per = percentile_doy(baseline, window=5, per=90)
    return atmos.tx90p(tasmax, tasmax_per, freq="YS")


def _tn10p(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    from xclim.core.calendar import percentile_doy

    tasmin = _tasmin(processed_dir, year)
    baseline = _var_baseline(processed_dir, "t2m", start_year, end_year, "t2m_min", "degC")
    tasmin_per = percentile_doy(baseline, window=5, per=10)
    return atmos.tn10p(tasmin, tasmin_per, freq="YS")


def _frost_days(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.frost_days(_tasmin(processed_dir, year), thresh="0 degC", freq="YS")


def _summer_days(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.tx_days_above(_tasmax(processed_dir, year), thresh="25 degC", freq="YS")


def _tropical_nights(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.tropical_nights(_tasmin(processed_dir, year), thresh="20 degC", freq="YS")


def _rx1day(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.max_1day_precipitation_amount(_pr(processed_dir, year), freq="YS")


def _rx5day(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.max_n_day_precipitation_amount(_pr(processed_dir, year), window=5, freq="YS")


def _sdii(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.daily_pr_intensity(_pr(processed_dir, year), thresh="1 mm/day", freq="YS")


def _cdd(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.maximum_consecutive_dry_days(_pr(processed_dir, year), thresh="1 mm/day", freq="YS")


def _cwd(processed_dir, year, start_year, end_year):
    import xclim.indicators.atmos as atmos
    return atmos.maximum_consecutive_wet_days(_pr(processed_dir, year), thresh="1 mm/day", freq="YS")


def _r95p(processed_dir, year, start_year, end_year):
    # R95p (ECA&D): annual total precip on wet days (>=1mm) exceeding the baseline 95th
    # percentile of wet-day precip. Computed manually (rather than a specific xclim
    # percentile-index function) because the R95p threshold is a single per-cell value,
    # not a day-of-year climatology.
    pr = _pr(processed_dir, year)
    pr_baseline = load_baseline(processed_dir, "tp", start_year, end_year)["tp_sum"]
    wet_baseline = pr_baseline.where(pr_baseline >= 1.0)
    threshold = wet_baseline.quantile(0.95, dim="time", skipna=True)
    exceed = pr.where((pr >= 1.0) & (pr > threshold))
    result = exceed.resample(time="YS").sum(skipna=True)
    result.attrs["units"] = "mm"
    result.name = "r95p"
    return result


def _agromet_daily(processed_dir: Path, year: int) -> xr.Dataset:
    ds = load_daily(processed_dir, "agromet", year)
    if ds is None:
        raise FileNotFoundError(
            f"Missing agromet daily data for {year} under {processed_dir}/agromet. "
            "Run the 'agromet' pipeline stage first."
        )
    return ds


def _annual_sum(da: xr.DataArray, year: int, name: str) -> xr.DataArray:
    result = da.sum(dim="time")
    result = result.expand_dims(time=[np.datetime64(f"{year}-01-01")])
    result.name = name
    return result


def _rain_days(processed_dir, year, start_year, end_year):
    ds = _agromet_daily(processed_dir, year)
    return _annual_sum(ds["rain_day"], year, "rain_days")


def _leaf_wetness_hours(processed_dir, year, start_year, end_year):
    ds = _agromet_daily(processed_dir, year)
    return _annual_sum(ds["leaf_wetness_hours"], year, "leaf_wetness_hours")


def _wind_driven_rain_hours(processed_dir, year, start_year, end_year):
    ds = _agromet_daily(processed_dir, year)
    return _annual_sum(ds["wind_driven_rain_hours"], year, "wind_driven_rain_hours")


def _longest_run_1d(x: np.ndarray) -> int:
    """Longest run of consecutive truthy values in a 1-D array."""
    max_run = run = 0
    for v in x:
        if v:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return max_run


def _longest_run(da_bool: xr.DataArray, year: int, name: str) -> xr.DataArray:
    result = xr.apply_ufunc(
        _longest_run_1d, da_bool,
        input_core_dims=[["time"]], vectorize=True, output_dtypes=[int],
    )
    result = result.expand_dims(time=[np.datetime64(f"{year}-01-01")])
    result.name = name
    return result


def _wet_spell_max(processed_dir, year, start_year, end_year):
    # Spell length resets at each calendar-year boundary (per-year agromet files),
    # unlike the senior pipeline's continuous point-based spell counter.
    ds = _agromet_daily(processed_dir, year)
    return _longest_run(ds["rain_day"] == 1, year, "wet_spell_max")


def _dry_spell_max(processed_dir, year, start_year, end_year):
    ds = _agromet_daily(processed_dir, year)
    return _longest_run(ds["rain_day"] == 0, year, "dry_spell_max")


INDICATOR_REGISTRY: dict[str, Callable] = {
    # generic ETCCDI/ECA&D climate indices
    "tx90p": _tx90p,
    "tn10p": _tn10p,
    "frost_days": _frost_days,
    "summer_days": _summer_days,
    "tropical_nights": _tropical_nights,
    "rx1day": _rx1day,
    "rx5day": _rx5day,
    "sdii": _sdii,
    "cdd": _cdd,
    "cwd": _cwd,
    "r95p": _r95p,
    # agromet / pest-disease-warning indices
    "rain_days": _rain_days,
    "leaf_wetness_hours": _leaf_wetness_hours,
    "wind_driven_rain_hours": _wind_driven_rain_hours,
    "wet_spell_max": _wet_spell_max,
    "dry_spell_max": _dry_spell_max,
}


def compute_indices_for_year(
    indices: list[str],
    year: int,
    processed_dir: Path,
    indices_dir: Path,
    normals_start_year: int,
    normals_end_year: int,
    log_dir: Path | None = None,
    defaults: dict[str, Any] | None = None,
    region_name: str = "",
) -> None:
    logger = get_logger("indices", log_dir)
    indices_dir.mkdir(parents=True, exist_ok=True)

    for name in indices:
        func = INDICATOR_REGISTRY.get(name)
        if func is None:
            logger.warning("No indicator registered for '%s', skipping", name)
            continue
        try:
            result = func(processed_dir, year, normals_start_year, normals_end_year)
        except Exception:
            logger.exception("Failed to compute index '%s' for %04d", name, year)
            continue

        result_ds = result.to_dataset(name=name)
        if defaults is not None:
            result_ds = stamp(result_ds, defaults, region_name, f"Annual index '{name}' -- {region_name} {year}",
                               extra_history=f"indices stage, index={name}, year={year}")

        out_file = indices_dir / f"{name}_{year}.nc"
        result_ds.to_netcdf(out_file)
        logger.info("Wrote %s", out_file)


def compute_all_indices(
    indices: list[str],
    start_year: int,
    end_year: int,
    processed_dir: Path,
    indices_dir: Path,
    normals_start_year: int,
    normals_end_year: int,
    log_dir: Path | None = None,
    defaults: dict[str, Any] | None = None,
    region_name: str = "",
) -> None:
    for year in range(start_year, end_year + 1):
        compute_indices_for_year(
            indices, year, processed_dir, indices_dir, normals_start_year, normals_end_year, log_dir,
            defaults, region_name,
        )


# =============================================================================
# Stage 6: PNG charts for sharing results outside the pipeline
# =============================================================================
# The NetCDF outputs from every other stage need code to read; these don't.
# Not part of the scientific record, just a rendering of it.

# Palette (validated: see dataviz skill references/palette.md)
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
SURFACE = "#fcfcfb"
TEMP_COLOR = "#eb6834"   # categorical slot 2 (orange)
RAIN_COLOR = "#2a78d6"   # categorical slot 1 (blue)

_SEQ_BLUE_STEPS = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#1c5cab", "#0d366b"]
SEQ_BLUE = LinearSegmentedColormap.from_list("seq_blue", _SEQ_BLUE_STEPS)


def _style_axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(BASELINE)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRIDLINE, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.title.set_color(INK_PRIMARY)


def plot_temperature_timeseries(processed_dir: Path, year: int, region_name: str, out_dir: Path) -> Path | None:
    ds = load_daily(processed_dir, "t2m", year)
    if ds is None:
        return None
    tmax = ds["t2m_max"].mean(dim=[d for d in ds.dims if d != "time"], skipna=True)
    tmin = ds["t2m_min"].mean(dim=[d for d in ds.dims if d != "time"], skipna=True)

    fig, ax = plt.subplots(figsize=(9, 4), facecolor=SURFACE)
    _style_axes(ax)
    ax.plot(tmax["time"].values, tmax.values, color=TEMP_COLOR, linewidth=2, solid_capstyle="round")
    ax.fill_between(tmax["time"].values, tmin.values, tmax.values, color=TEMP_COLOR, alpha=0.12, linewidth=0)
    ax.set_title(f"Daily temperature -- {region_name} {year}", fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel("degC", color=INK_SECONDARY, fontsize=9)
    fig.text(0.99, 0.01, "line: daily max, shaded band: daily min-max range",
              ha="right", fontsize=8, color=INK_MUTED)
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"temperature_{year}.png"
    fig.savefig(out_file, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out_file


def plot_rainfall_timeseries(processed_dir: Path, year: int, region_name: str, out_dir: Path) -> Path | None:
    ds = load_daily(processed_dir, "tp", year)
    if ds is None:
        return None
    rain = ds["tp_sum"].mean(dim=[d for d in ds.dims if d != "time"], skipna=True)

    fig, ax = plt.subplots(figsize=(9, 4), facecolor=SURFACE)
    _style_axes(ax)
    ax.bar(rain["time"].values, rain.values, color=RAIN_COLOR, width=1.0, linewidth=0)
    ax.set_title(f"Daily rainfall -- {region_name} {year}", fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel("mm", color=INK_SECONDARY, fontsize=9)
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"rainfall_{year}.png"
    fig.savefig(out_file, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out_file


def plot_index_map(indices_dir: Path, index_name: str, year: int, region: Region, out_dir: Path) -> Path | None:
    f = indices_dir / f"{index_name}_{year}.nc"
    if not f.exists():
        return None
    ds = xr.open_dataset(f)
    da = ds[index_name].isel(time=0) if "time" in ds[index_name].dims else ds[index_name]

    x_dim = "longitude" if "longitude" in da.dims else "lon"
    y_dim = "latitude" if "latitude" in da.dims else "lat"

    fig, ax = plt.subplots(figsize=(6, 5.5), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    mesh = ax.pcolormesh(da[x_dim], da[y_dim], da.values, cmap=SEQ_BLUE, shading="auto")
    cbar = fig.colorbar(mesh, ax=ax, shrink=0.85, pad=0.03)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(colors=INK_MUTED, labelsize=8)
    units = da.attrs.get("units", "")
    cbar.set_label(f"{da.attrs.get('long_name', index_name)}" + (f" ({units})" if units else ""),
                    color=INK_SECONDARY, fontsize=8)

    if region.polygon is not None:
        xs, ys = region.polygon.exterior.xy
        ax.plot(xs, ys, color=INK_PRIMARY, linewidth=1.5)

    ax.set_title(f"{index_name} -- {region.name} {year}", fontsize=12, fontweight="bold", loc="left")
    ax.set_xlabel("longitude", color=INK_SECONDARY, fontsize=8)
    ax.set_ylabel("latitude", color=INK_SECONDARY, fontsize=8)
    ax.tick_params(colors=INK_MUTED, labelsize=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"map_{index_name}_{year}.png"
    fig.savefig(out_file, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out_file


def build_indices_summary(indices_dir: Path, year: int, out_dir: Path) -> Path | None:
    rows = []
    for f in sorted(indices_dir.glob(f"*_{year}.nc")):
        name = f.stem.rsplit(f"_{year}", 1)[0]
        ds = xr.open_dataset(f)
        da = ds[name]
        vals = da.values
        vals = vals[~np.isnan(vals)]
        if len(vals) == 0:
            continue
        units = da.attrs.get("units", "")
        rows.append((name, float(np.mean(vals)), float(np.min(vals)), float(np.max(vals)), units))

    if not rows:
        return None

    fig, ax = plt.subplots(figsize=(8, 0.35 * len(rows) + 1), facecolor=SURFACE)
    ax.axis("off")
    col_labels = ["Index", "Mean", "Min", "Max", "Units"]
    col_widths = [0.34, 0.16, 0.16, 0.16, 0.18]
    cell_text = [[n, f"{m:.1f}", f"{mn:.1f}", f"{mx:.1f}", u] for n, m, mn, mx, u in rows]
    table = ax.table(cellText=cell_text, colLabels=col_labels, colWidths=col_widths,
                      loc="center", cellLoc="left", colLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor(GRIDLINE)
        cell.set_text_props(color=INK_PRIMARY)
        if r == 0:
            cell.set_text_props(color=INK_SECONDARY, fontweight="bold")
            cell.set_facecolor(SURFACE)
        else:
            cell.set_facecolor(SURFACE)
    ax.set_title(f"Index summary -- {year} (spatial mean/min/max across grid cells)",
                 fontsize=11, fontweight="bold", loc="left", color=INK_PRIMARY)
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"indices_summary_{year}.png"
    fig.savefig(out_file, dpi=150, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)

    csv_file = out_dir / f"indices_summary_{year}.csv"
    with open(csv_file, "w", encoding="utf-8") as fh:
        fh.write(",".join(col_labels) + "\n")
        for row in cell_text:
            fh.write(",".join(row) + "\n")

    return out_file


def generate_all_plots(
    defaults: dict[str, Any],
    year: int,
    processed_dir: Path,
    indices_dir: Path,
    plots_dir: Path,
    region: Region,
    log_dir: Path | None = None,
) -> None:
    logger = get_logger("plots", log_dir)

    f = plot_temperature_timeseries(processed_dir, year, region.name, plots_dir)
    logger.info("Wrote %s", f) if f else logger.warning("No t2m data for %d, skipped temperature plot", year)

    f = plot_rainfall_timeseries(processed_dir, year, region.name, plots_dir)
    logger.info("Wrote %s", f) if f else logger.warning("No tp data for %d, skipped rainfall plot", year)

    for index_name in ("summer_days", "rx1day"):
        f = plot_index_map(indices_dir, index_name, year, region, plots_dir)
        if f:
            logger.info("Wrote %s", f)

    f = build_indices_summary(indices_dir, year, plots_dir)
    logger.info("Wrote %s", f) if f else logger.warning("No indices found for %d, skipped summary table", year)


# =============================================================================
# Run context: turns (defaults, region, variables, years) into a dispatch table
# shared by both the flag-driven CLI and the interactive menu below
# =============================================================================

STAGE_NAMES = ["download", "preprocess", "agromet", "normals", "anomalies", "indices", "plots", "all"]


def build_paths(out_dir: Path, region_name: str) -> dict[str, Path]:
    root = Path(out_dir) / region_name
    paths = {
        "raw_dir": root / "raw",
        "processed_dir": root / "processed",
        "normals_dir": root / "normals",
        "anomalies_dir": root / "anomalies",
        "indices_dir": root / "indices",
        "plots_dir": root / "plots",
        "log_dir": root / "logs",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def run_stage(
    stage: str,
    defaults: dict[str, Any],
    region: Region,
    variables: list[str],
    start_year: int,
    end_year: int,
    normals_start: int,
    normals_end: int,
    indices: list[str],
    paths: dict[str, Path],
    dataset: str | None = None,
    parallel: int | None = None,
) -> None:
    def run_download():
        download_all(defaults, region, variables, start_year, end_year,
                      paths["raw_dir"], dataset=dataset, log_dir=paths["log_dir"], parallel=parallel)

    def run_preprocess():
        preprocess_all(defaults, variables, start_year, end_year,
                        paths["raw_dir"], paths["processed_dir"], region, log_dir=paths["log_dir"])

    def run_agromet():
        compute_agromet_all(defaults, start_year, end_year,
                             paths["raw_dir"], paths["processed_dir"], region, log_dir=paths["log_dir"])

    def run_normals():
        compute_normals(variables, paths["processed_dir"], paths["normals_dir"],
                         normals_start, normals_end,
                         smoothing_window_days=defaults["normals_smoothing_window_days"], log_dir=paths["log_dir"],
                         defaults=defaults, region_name=region.name)

    def run_anomalies():
        compute_all_anomalies(variables, start_year, end_year,
                               paths["processed_dir"], paths["normals_dir"], paths["anomalies_dir"],
                               normals_start, normals_end, log_dir=paths["log_dir"],
                               defaults=defaults, region_name=region.name)

    def run_indices():
        compute_all_indices(indices, start_year, end_year,
                             paths["processed_dir"], paths["indices_dir"],
                             normals_start, normals_end, log_dir=paths["log_dir"],
                             defaults=defaults, region_name=region.name)

    def run_plots():
        for year in range(start_year, end_year + 1):
            generate_all_plots(defaults, year, paths["processed_dir"], paths["indices_dir"], paths["plots_dir"],
                                region, log_dir=paths["log_dir"])

    stages = {
        "download": run_download,
        "preprocess": run_preprocess,
        "agromet": run_agromet,
        "normals": run_normals,
        "anomalies": run_anomalies,
        "indices": run_indices,
        "plots": run_plots,
    }

    if stage == "all":
        for name, fn in stages.items():
            print(f"--- Running stage: {name} ---")
            fn()
    else:
        stages[stage]()


# =============================================================================
# Interactive menu -- run with no arguments to be walked through the choices
# =============================================================================

def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default
        print("  This is required -- please type something.")


def _ask_float(prompt: str) -> float:
    while True:
        raw = _ask(prompt)
        try:
            return float(raw)
        except ValueError:
            print("  That's not a number -- try again, e.g. 21.5")


def _ask_bbox() -> tuple[float, float, float, float]:
    """North West South East as one line (space- or comma-separated), or fall
    back to asking each edge separately if that's easier to get right."""
    while True:
        raw = _ask("  North West South East (e.g. 21.3 78.9 20.9 79.3)")
        parts = raw.replace(",", " ").split()
        if len(parts) == 4:
            try:
                north, west, south, east = (float(p) for p in parts)
                return north, west, south, east
            except ValueError:
                pass
        print("  Couldn't read 4 numbers from that -- type them separated by spaces, e.g. 21.3 78.9 20.9 79.3")


def _ask_int(prompt: str, default: int) -> int:
    raw = _ask(prompt, default=str(default))
    try:
        return int(raw)
    except ValueError:
        print(f"  '{raw}' isn't a whole number, using {default}.")
        return default


def _ask_choice(prompt: str, options: list[str], default_index: int = 0) -> str:
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        marker = " (default)" if i - 1 == default_index else ""
        print(f"  {i}) {opt}{marker}")
    while True:
        raw = input(f"Choose 1-{len(options)} [{default_index + 1}]: ").strip()
        if not raw:
            return options[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  Invalid choice, try again.")


def _ask_yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{d}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please answer y or n.")


def run_interactive() -> None:
    print("=" * 60)
    print("ERA5 Pipeline -- interactive mode")
    print("Answer each question, or press Enter to accept the [default].")
    print("=" * 60)

    defaults = load_defaults()

    stage = _ask_choice("Which stage do you want to run?", STAGE_NAMES, default_index=len(STAGE_NAMES) - 1)

    loc_choice = _ask_choice(
        "How do you want to specify the location?",
        ["Boundary file (.kml / .shp / .geojson)", "Bounding box (type lat/long numbers myself)"],
    )

    boundary_file = None
    bbox = None
    if loc_choice.startswith("Boundary"):
        boundary_file = _ask("Path to boundary file (e.g. Pandhurna.kml)")
    else:
        print("\nEnter the bounding box in degrees (latitude range roughly 8-37 and longitude 68-97 for India).")
        bbox = _ask_bbox()

    default_region_name = Path(boundary_file).stem if boundary_file else "custom_region"
    region_name = _ask("Region name (used for the output folder name)", default=default_region_name)

    region = resolve_region(
        bbox=bbox, boundary_file=boundary_file, name=region_name,
        buffer_deg=defaults["boundary_buffer_deg"],
    )

    all_vars = list(defaults["variables"].values())
    print(f"\nAvailable variables: {', '.join(all_vars)}")
    var_input = _ask("Which variables? (comma-separated)", default=",".join(defaults["default_variables"]))
    variables = [v.strip() for v in var_input.split(",") if v.strip()]

    start_year = _ask_int("Start year", default=defaults["normals_start_year"])
    end_year = _ask_int("End year", default=defaults["normals_end_year"])

    normals_start = defaults["normals_start_year"]
    normals_end = defaults["normals_end_year"]
    if stage in ("normals", "anomalies", "indices", "all"):
        print("\nClimatic normals need a baseline period (WMO standard: 1991-2020).")
        print("For a quick test with a short date range, you can set this to match your start/end year instead.")
        normals_start = _ask_int("Normals baseline start year", default=normals_start)
        normals_end = _ask_int("Normals baseline end year", default=normals_end)

    indices = defaults["indices"]
    if stage in ("indices", "all"):
        print(f"\nAvailable indices: {', '.join(defaults['indices'])}")
        idx_input = _ask("Which indices? (comma-separated)", default=",".join(defaults["indices"]))
        indices = [i.strip() for i in idx_input.split(",") if i.strip()]

    parallel = defaults.get("cds_parallel", 4)
    if stage in ("download", "all"):
        parallel = _ask_int("Parallel CDS downloads (higher = faster, but don't overload the server)", default=parallel)

    out_dir = Path(_ask("Output directory", default="data"))

    print("\n" + "-" * 60)
    print("SUMMARY")
    print("-" * 60)
    print(f"  Stage:           {stage}")
    print(f"  Region:          {region.name}")
    print(f"  Area (N,W,S,E):  {[round(v, 3) for v in region.area]}")
    print(f"  Years:           {start_year}-{end_year}")
    print(f"  Variables:       {variables}")
    if stage in ("normals", "anomalies", "indices", "all"):
        print(f"  Normals baseline:{normals_start}-{normals_end}")
    if stage in ("indices", "all"):
        print(f"  Indices:         {indices}")
    print(f"  Output folder:   {out_dir / region.name}")
    print("-" * 60)

    if not _ask_yesno("Proceed?", default=True):
        print("Cancelled -- nothing was run.")
        return

    paths = build_paths(out_dir, region.name)
    run_stage(stage, defaults, region, variables, start_year, end_year,
              normals_start, normals_end, indices, paths, parallel=parallel)
    print("\nDone. Results are in:", out_dir / region.name)


# =============================================================================
# Flag-driven CLI -- for scripting / repeat runs
# =============================================================================

def _add_common_args(p: argparse.ArgumentParser, require_years: bool) -> None:
    loc = p.add_mutually_exclusive_group(required=True)
    loc.add_argument("--boundary", help="Boundary file (.shp/.geojson/.kml/...), any format geopandas reads")
    loc.add_argument(
        "--bbox", nargs=4, type=float, metavar=("NORTH", "WEST", "SOUTH", "EAST"),
        help="Plain bounding box in degrees",
    )
    p.add_argument("--region-name", default=None, help="Label for output subfolder (default: derived from --boundary, or 'custom_region')")
    p.add_argument("--variables", default=None, help="Comma-separated short variable names, e.g. t2m,tp,u10,v10,d2m (default: config/defaults.yaml default_variables)")
    p.add_argument("--out-dir", default="data", help="Root output directory (default: data/)")
    p.add_argument("--dataset", default=None, help="CDS dataset id (default: config/defaults.yaml 'dataset')")
    p.add_argument("--parallel", type=int, default=None, help="Concurrent CDS download requests (default: config/defaults.yaml cds_parallel)")
    p.add_argument("--buffer-deg", type=float, default=None, help="Degrees of padding around a --boundary's bbox before download (default: config/defaults.yaml)")
    p.add_argument("--normals-start", type=int, default=None, help="Normals baseline start year (default: config/defaults.yaml)")
    p.add_argument("--normals-end", type=int, default=None, help="Normals baseline end year (default: config/defaults.yaml)")
    p.add_argument(
        "--start", type=int, default=None, required=require_years,
        help="Start year" + ("" if require_years else " (default: normals baseline start)"),
    )
    p.add_argument(
        "--end", type=int, default=None, required=require_years,
        help="End year" + ("" if require_years else " (default: normals baseline end)"),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="ERA5/ERA5-Land download, preprocessing, agromet, normals, anomalies, indices and plots. "
                     "Run with no arguments for an interactive menu.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in STAGE_NAMES:
        require_years = name != "normals"
        p = sub.add_parser(name)
        _add_common_args(p, require_years)
        if name == "indices":
            p.add_argument("--indices", default=None, help="Comma-separated indicator names (default: config/defaults.yaml indices)")

    sub.add_parser("interactive", help="Launch the interactive menu (same as running with no arguments)")

    return parser


def main() -> None:
    if len(sys.argv) == 1:
        run_interactive()
        return

    args = _build_parser().parse_args()

    if args.command == "interactive":
        run_interactive()
        return

    defaults = load_defaults()
    bbox = tuple(args.bbox) if args.bbox else None
    region = resolve_region(
        bbox=bbox, boundary_file=args.boundary, name=args.region_name,
        buffer_deg=args.buffer_deg if args.buffer_deg is not None else defaults["boundary_buffer_deg"],
    )

    variables = args.variables.split(",") if args.variables else defaults["default_variables"]
    variables = [v.strip() for v in variables]

    normals_start = args.normals_start if args.normals_start is not None else defaults["normals_start_year"]
    normals_end = args.normals_end if args.normals_end is not None else defaults["normals_end_year"]
    start_year = args.start if args.start is not None else normals_start
    end_year = args.end if args.end is not None else normals_end

    indices = args.indices.split(",") if getattr(args, "indices", None) else defaults["indices"]
    indices = [i.strip() for i in indices]

    paths = build_paths(Path(args.out_dir), region.name)
    run_stage(args.command, defaults, region, variables, start_year, end_year,
              normals_start, normals_end, indices, paths, dataset=args.dataset, parallel=args.parallel)


if __name__ == "__main__":
    main()
