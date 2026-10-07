"""Download continuous 2019-2022 for the whole Pandhurna box (AREA), into era5_raw --
the same folder/area/request-shape the per-event downloads already use successfully,
just not tied to any one event's incident window. Run with:
    python backtest/download_2019_2022.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt

if __name__ == "__main__":
    bt.MONTHS_PER_REQUEST = 1
    variables = bt.rule_vars(["G-D02", "P01", "G-P01", "G-P03", "X-W01"])
    months = bt.months_between(date(2019, 1, 1), date(2022, 12, 1))
    # bt.AREA's north edge (21.8) cuts off the northern ~0.11 deg of Pandhurna's real
    # boundary (KML bounds go up to lat 21.913) -- bumped to 22.0 here (not in bt.AREA
    # itself, so per-event downloads/analyses elsewhere are unaffected) to fully
    # contain the boundary with a small margin. All other edges already covered it.
    area = [22.0, bt.AREA[1], bt.AREA[2], bt.AREA[3]]
    print(f"2019-2022 download: {len(variables)} variables x {len(months)} months, box {area}")
    # parallel=4: confirmed by testing that 8 trips "too many queued requests"
    # for this account -- 4 is the ceiling that actually runs clean right now.
    for v in variables:
        print(f"--- variable {v} ---")
        bt.download([v], months, raw_dir=bt.RAW_DIR, area=area, parallel=4)
    print("2019-2022 download complete.")
