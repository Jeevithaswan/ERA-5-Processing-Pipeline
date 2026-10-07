"""Download raw hourly ERA5-Land for Chhindwara, 1991-2020 (WMO 30-year normals
baseline), for the manager's anomaly-detection request -- RAW data (not CDS's
pre-aggregated daily statistics), so it goes through the exact same hourly-to-daily
aggregation this project already uses for Pandhurna (correct RH from hourly T/Td,
correct wind speed from hourly u/v, no methodological drift from a faster shortcut).

Same request shape already proven reliable for Pandhurna: one variable, one month
per request, parallel=4 (the confirmed ceiling before "too many queued requests"
rejections start). This is a much bigger job than Pandhurna's 4-year test --
360 months x 7 variables = 2520 month-files, roughly 7.5x the request count that
took the better part of a day for Pandhurna -- expect this to run for a long
stretch; it's resumable, so stopping and restarting never loses progress.

Run with: python backtest/download_chhindwara_1991_2020.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt

RAW_DIR = bt.BT_DIR / "chhindwara_raw"  # separate from era5_raw (Pandhurna) and
                                        # chhindwara_normals_raw (the abandoned
                                        # daily-statistics attempt) -- different box,
                                        # different data, must not share a folder.
# Chhindwara's real boundary (Chhindwara.kml) is 78.24-79.41 deg E, 21.46-22.82 deg N;
# this box adds margin on every side, same convention as every other box in this project.
AREA = [22.9, 78.1, 21.4, 79.5]  # North, West, South, East

if __name__ == "__main__":
    bt.MONTHS_PER_REQUEST = 1
    variables = ["t2m", "d2m", "tp", "u10", "v10", "ssrd", "sp"]
    months = bt.months_between(date(1991, 1, 1), date(2020, 12, 1))
    print(f"Chhindwara 1991-2020: {len(variables)} variables x {len(months)} months "
         f"= {len(variables) * len(months)} month-files, box {AREA}")
    print("This is a large, long-running download (~7.5x Pandhurna's 2019-2022 pull). "
         "Resumable -- safe to stop and restart any time.")
    for v in variables:
        print(f"--- variable {v} ---")
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        bt.download([v], months, raw_dir=RAW_DIR, area=AREA, parallel=4)
    print("Chhindwara 1991-2020 raw download complete.")
