"""Download 1991-2020 (WMO normals baseline) daily ERA5-Land statistics for
Chhindwara, for the manager's 30-year anomaly-detection request -- NOT the hourly
reanalysis-era5-land dataset used elsewhere in this project. Uses CDS's
'derived-era5-land-daily-statistics' dataset instead: it gives daily mean/max/min/
sum computed server-side, so one request can cover a full year instead of the
1-month-at-a-time limit hourly data needed, cutting total request count roughly
10x for the same calendar span.

Trade-off worth knowing: because the daily stat is computed per variable
independently (not from hourly RH, wind speed, etc. the way the rest of this
pipeline does it), anything DERIVED from combining variables (relative humidity,
leaf-wetness hours, the water-balance calc) would be a small methodological
approximation if built from this data instead of hourly. Fine for climatological
normals/anomalies (the actual ask), not meant to replace the hourly pipeline for
precise daily agromet indicators.

Resumable: skips any (variable, statistic, year) file already on disk. Automatic
chunk-splitting: a request CDS rejects as too large is retried as two half-year
chunks instead of needing the size found by hand first.

Run with: python backtest/download_chhindwara_normals.py
"""
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt  # for CDS_NAMES and the project's logging helper
import pipeline as pl

BT_DIR = Path(__file__).resolve().parent
OUT_DIR = BT_DIR / "chhindwara_normals_raw"
LOG_DIR = BT_DIR / "logs"
OUT_DIR.mkdir(exist_ok=True)

START_YEAR, END_YEAR = 1991, 2020
# Chhindwara's real boundary (from Chhindwara.kml) is 78.24-79.41 deg E, 21.46-22.82
# deg N -- this box adds margin on every side, same convention as the Pandhurna work.
AREA = [22.9, 78.1, 21.4, 79.5]  # North, West, South, East

# (variable, CDS daily_statistic, output-filename suffix). t2m needs three separate
# statistics (mean/max/min), everything else needs just the one that makes sense for
# it -- tp and ssrd are accumulated fields, so sum, not mean, is the meaningful daily stat.
JOBS = [
    ("t2m", "daily_mean", "mean"), ("t2m", "daily_maximum", "max"), ("t2m", "daily_minimum", "min"),
    ("d2m", "daily_mean", "mean"),
    ("u10", "daily_mean", "mean"),
    ("v10", "daily_mean", "mean"),
    ("sp", "daily_mean", "mean"),
    ("tp", "daily_sum", "sum"),
    ("ssrd", "daily_sum", "sum"),
]


def _out_file(short: str, suffix: str, year: int, months: list[int]) -> Path:
    if len(months) == 12:
        return OUT_DIR / f"{short}_{suffix}_{year}.nc"
    return OUT_DIR / f"{short}_{suffix}_{year}_{months[0]:02d}-{months[-1]:02d}.nc"


def fetch(short: str, stat: str, suffix: str, year: int, months: list[int], logger, retries_left: int = 4):
    import cdsapi
    out = _out_file(short, suffix, year, months)
    label = f"{short} {suffix} {year} months {months[0]}-{months[-1]}"
    if out.exists():
        return label, True
    req = {
        "variable": [bt.CDS_NAMES[short]],
        "year": [str(year)],
        "month": [f"{m:02d}" for m in months],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "daily_statistic": stat,
        "time_zone": "utc+00:00",
        "frequency": "1_hourly",
        "area": AREA,
    }
    try:
        logger.info("Requesting %s", label)
        cdsapi.Client(quiet=True, progress=False).retrieve(
            "derived-era5-land-daily-statistics", req, str(out) + ".part")
        os.replace(str(out) + ".part", out)
        logger.info("Done %s", label)
        return label, True
    except Exception as e:
        msg = str(e)
        logger.exception("Failed %s", label)
        if "temporarily limited" in msg or "queued requests" in msg:
            if retries_left <= 0:
                logger.error("Giving up on %s: still rate-limited after all retries", label)
                return label, False
            wait_s = 60 * (5 - retries_left)
            logger.info("Rate-limited on %s -- waiting %ds before resubmitting unchanged", label, wait_s)
            time.sleep(wait_s)
            return fetch(short, stat, suffix, year, months, logger, retries_left - 1)
        if "too large" in msg and len(months) > 1:
            mid = len(months) // 2
            logger.info("Retrying %s split into %s + %s", label, months[:mid], months[mid:])
            r1 = fetch(short, stat, suffix, year, months[:mid], logger, retries_left)
            time.sleep(2)
            r2 = fetch(short, stat, suffix, year, months[mid:], logger, retries_left)
            return label, r1[1] and r2[1]
        return label, False


def main():
    logger = pl.get_logger("download_chhindwara_normals", LOG_DIR)
    years = list(range(START_YEAR, END_YEAR + 1))
    # year outermost, variable-statistic innermost: so one round covers every
    # variable for one year before moving to the next year, instead of finishing
    # t2m's full 30-year history before any other variable even starts.
    jobs = [(short, stat, suffix, year, list(range(1, 13)))
           for year in years for short, stat, suffix in JOBS]
    jobs = [j for j in jobs if not _out_file(j[0], j[2], j[3], j[4]).exists()]
    print(f"Chhindwara 1991-2020 daily statistics: {len(JOBS)} variable-statistic combos "
         f"x {len(years)} years, {len(jobs)} requests still needed (before any auto-splitting), box {AREA}")
    # Small fixed batches (4 at a time), each fully finished before the next starts --
    # not one big thread pool pre-queuing all requests, so at most 4 are ever in
    # flight against the account at once and progress is checkable between batches.
    batch_size = 4
    all_failed = []
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]
        print(f"\n--- batch {i // batch_size + 1} of {(len(jobs) + batch_size - 1) // batch_size} "
             f"({len(batch)} requests) ---")
        with ThreadPoolExecutor(max_workers=batch_size) as ex:
            results = list(ex.map(lambda j: fetch(*j, logger), batch))
        all_failed += [label for label, ok in results if not ok]
    if all_failed:
        print(f"\n{len(all_failed)} failed: {all_failed}")
        raise SystemExit(1)
    print("\nChhindwara 1991-2020 daily statistics download complete.")


if __name__ == "__main__":
    main()
