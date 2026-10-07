"""Kick off the spatial multi-year download: I01's variables, Jul 2018 - Dec 2022,
wide box covering all of Pandhurna, separate raw folder -- see WIDE_AREA/SPATIAL_RAW_DIR
in backtest.py. Run with: python backtest/download_spatial.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt

if __name__ == "__main__":
    # This box (Pandhurna + Chhindwara) reliably rejects 3-month bundles as too
    # large, so every one would fail once and retry anyway -- go straight to
    # 1-month requests instead of burning a guaranteed-fail attempt on each.
    bt.MONTHS_PER_REQUEST = 1
    variables = bt.rule_vars(["G-D02", "P01", "G-P01", "G-P03", "X-W01"])
    # 2019-2022 (what's actually being tested) requested before the Jul-Dec 2018
    # lead-in (only needed to let the X-W01 water balance settle before 2019) --
    # CDS's global queue right now is thousands deep with no visibility into wait
    # time, so whatever clears first should be a year we actually care about.
    priority_months = bt.months_between(date(2019, 1, 1), date(2022, 12, 1))
    leadin_months = bt.months_between(date(2018, 7, 1), date(2018, 12, 1))
    months = priority_months + leadin_months
    print(f"Spatial download: {len(variables)} variables x {len(months)} months, box {bt.WIDE_AREA}")
    # One variable per request, not all 7 bundled: on this wide box, 7-variable/1-month
    # requests were rejected outright by CDS every single time ("cost limits exceeded"),
    # unlike the small per-event box where bundled requests at least sometimes went
    # through. Single-variable/1-month requests are exactly the shape that succeeded
    # reliably for the per-event downloads (era5_raw), so match that here instead of
    # retrying a request shape that has a 0% success rate on this box.
    #
    # parallel=1 (fully serial), confirmed necessary by testing: even 4 concurrent
    # single-variable/single-month requests (individually tiny) were instantly
    # rejected with "Number queued requests for this dataset is temporarily limited" --
    # so this account/dataset is being limited purely on concurrent request COUNT
    # right now, not on request size. One at a time avoids tripping that limit.
    for v in variables:
        print(f"--- variable {v} ---")
        bt.download([v], months, raw_dir=bt.SPATIAL_RAW_DIR, area=bt.WIDE_AREA, parallel=1)
    print("Spatial download complete.")
