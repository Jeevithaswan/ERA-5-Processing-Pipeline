"""Generalized ERA5-Land climatology pipeline -- any place, any duration, any
variables, any indices.

Download -> daily processing -> WMO normals -> anomalies -> drought indices ->
research-grade figures, for ANY region (bounding box or boundary file: KML/
shapefile/GeoJSON). The underlying building blocks (pipeline.py's region
resolution, daily processing, normals, anomalies; indices.py's SPI/SPEI/PNI/
RAI/PCI/AI/EDDI/water-balance/IMD-departure formulas and figure functions)
are place-agnostic -- nothing in them is hardcoded to any one location. This
script is the orchestration layer: one CLI command that wires those pieces
together for a region, date range, variable set, and index set all supplied
as arguments.

Variables and indices are both fully selectable -- nothing is downloaded or
computed unless asked for. Each requested index is independently gated on
whether its required variables are actually available; an index that can't
be computed is skipped with a printed reason, never a crash.

Index -> required variable(s):
  spi, pni, rai, pci, imd-departure, weekly, fortnightly, etccdi  -> tp
  spei, eddi, ai, water-balance                                  -> tp + t2m,
      upgraded to full FAO-56 Penman-Monteith PET if d2m,u10,v10,ssrd,sp are
      ALL also present, else falls back to the temperature-only Hargreaves
      method (noted in the output).

Examples
--------
# A new place, by bounding box, 10 years, just SPI + temperature:
python run_climatology.py --place "MyTown" --bbox 23.0 78.0 22.0 79.0 \\
    --start-year 2014 --end-year 2023 --variables t2m,tp --indices spi \\
    --out-dir data/MyTown

# Full index set (incl. Penman-Monteith PET) for an already-downloaded region:
python run_climatology.py --place "Chhindwara" --boundary-file Chhindwara.kml \\
    --start-year 1991 --end-year 2026 --raw-dir backtest/chhindwara_raw \\
    --variables t2m,tp,d2m,u10,v10,ssrd,sp --indices all --skip-download

Run with --skip-download to only (re)build normals/anomalies/indices/figures
from whatever is already on disk -- useful for a scheduled "regenerate the
report" run that shouldn't re-hit the CDS API every time.
"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "backtest"))
import pipeline as pl
import backtest as bt
import indices as idx

ALL_INDICES = ["spi", "spei", "pni", "rai", "pci", "ai", "eddi", "water-balance",
                "imd-departure", "weekly", "fortnightly", "etccdi"]
# Indices needing only rainfall vs. needing rainfall+temperature(+PET).
TP_ONLY_INDICES = {"spi", "pni", "rai", "pci", "imd-departure", "weekly", "fortnightly", "etccdi"}
PET_INDICES = {"spei", "eddi", "ai", "water-balance"}
PET_EXTRA_VARS = {"d2m", "u10", "v10", "ssrd", "sp"}


def run(place: str, region: pl.Region, variables: list[str], requested_indices: list[str],
        start_year: int, end_year: int, baseline_start: int, baseline_end: int,
        raw_dir: Path, out_dir: Path, parallel: int, skip_download: bool) -> None:
    processed_dir = out_dir / "processed"
    normals_dir = out_dir / "normals"
    anomalies_dir = out_dir / "anomalies"
    indices_dir = out_dir / "drought_indices"
    annual_indices_dir = out_dir / "annual_indices"
    fig_dir = out_dir / "figures"
    for d in (processed_dir, normals_dir, anomalies_dir, indices_dir, annual_indices_dir, fig_dir):
        d.mkdir(parents=True, exist_ok=True)

    varset = set(variables)
    have_tp, have_t2m = "tp" in varset, "t2m" in varset
    have_full_pet = PET_EXTRA_VARS <= varset

    wanted_tp_only = [i for i in requested_indices if i in TP_ONLY_INDICES]
    wanted_pet = [i for i in requested_indices if i in PET_INDICES]
    skip_tp_only = [i for i in wanted_tp_only if not have_tp]
    skip_pet = [i for i in wanted_pet if not (have_tp and have_t2m)]
    wanted_tp_only = [i for i in wanted_tp_only if i not in skip_tp_only]
    wanted_pet = [i for i in wanted_pet if i not in skip_pet]
    for i in skip_tp_only:
        print(f"[{place}] Skipping '{i}': needs tp, which isn't in --variables ({variables}).")
    for i in skip_pet:
        print(f"[{place}] Skipping '{i}': needs both t2m and tp, which isn't in --variables ({variables}).")
    if wanted_pet and not have_full_pet:
        print(f"[{place}] {sorted(PET_EXTRA_VARS - varset)} not in --variables -- {wanted_pet} will use the "
              f"temperature-only Hargreaves PET approximation instead of full FAO-56 Penman-Monteith.")

    defaults = pl.load_defaults()
    months = bt.months_between(pd.Timestamp(start_year, 1, 1).date(), pd.Timestamp(end_year, 12, 1).date())

    if not skip_download:
        print(f"[{place}] Downloading {variables} for {start_year}-{end_year} ({len(months)} months)...")
        bt.download(variables, months, raw_dir=raw_dir, area=region.area, parallel=parallel)
    else:
        print(f"[{place}] --skip-download set, using whatever raw data is already on disk.")

    print(f"[{place}] Preprocessing to daily + clipping to region...")
    for short in variables:
        for year in range(start_year, end_year + 1):
            pl.process_variable_year(defaults, short, year, raw_dir, processed_dir, region)

    print(f"[{place}] Computing WMO {baseline_start}-{baseline_end} normals + anomalies...")
    pl.compute_normals(variables, processed_dir, normals_dir, baseline_start, baseline_end,
                        smoothing_window_days=5, defaults=defaults, region_name=place)
    pl.compute_all_anomalies(variables, start_year, end_year, processed_dir, normals_dir,
                              anomalies_dir, baseline_start, baseline_end, defaults=defaults,
                              region_name=place)

    if not have_tp:
        print(f"[{place}] 'tp' not in --variables -- no drought indices can be computed, only the "
              f"normals/anomalies already written to {anomalies_dir}.")
        print(f"\nDone. Outputs in: {out_dir}")
        return

    tp_daily = idx.load_area_averaged_daily(processed_dir, "tp", "tp_sum")
    tp_monthly = tp_daily.resample("MS").sum()

    pet_monthly = water_balance_monthly = None
    if wanted_pet:
        tmax_daily = idx.load_area_averaged_daily(processed_dir, "t2m", "t2m_max")
        tmin_daily = idx.load_area_averaged_daily(processed_dir, "t2m", "t2m_min")
        tmean_daily = idx.load_area_averaged_daily(processed_dir, "t2m", "t2m_mean")
        if have_full_pet:
            print(f"[{place}] Computing PET (FAO-56 Penman-Monteith)...")
            pet_daily = idx.penman_monteith_pet_daily(region, raw_dir, start_year, end_year)
        else:
            print(f"[{place}] Computing PET (Hargreaves, temperature-only fallback)...")
            lat_deg = (region.area[0] + region.area[2]) / 2.0  # mean of North/South bounds
            pet_daily = idx.hargreaves_pet_daily(tmax_daily, tmin_daily, tmean_daily, lat_deg)
        pet_monthly = pet_daily.resample("MS").sum()
        water_balance_monthly = tp_monthly - pet_monthly

    n_years = baseline_end - baseline_start + 1
    pet_note = ("full FAO-56 Penman-Monteith" if have_full_pet else "temperature-only Hargreaves approximation") \
        if wanted_pet else "not computed (no PET-dependent index requested)"
    baseline_note = (f"Baseline: {baseline_start}-{baseline_end} ({n_years} years), region: {place}. "
                      f"PET method: {pet_note}. SPEI is standardized via a per-calendar-month z-score, not the "
                      f"published method's 3-parameter log-logistic fit. The IMD departure index matches IMD "
                      f"Pune's own category thresholds, not necessarily IMD's own baseline years.")

    tables: dict[str, pd.DataFrame] = {}
    F = idx.FORMULAS
    bl = f"{baseline_start}-{baseline_end}"

    if "spi" in wanted_tp_only:
        print(f"[{place}] Computing SPI...")
        t = pd.DataFrame({"precip_mm_1mo": tp_monthly})
        for scale in idx.SPI_SCALES:
            t[f"precip_mm_{scale}mo"] = tp_monthly.rolling(scale, min_periods=scale).sum()
            t[f"SPI{scale}"] = idx.compute_spi(tp_monthly, scale)
            t[f"SPI{scale}_category"] = t[f"SPI{scale}"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
        tables["SPI"] = t
        idx.plot_simple_category_bars(t["SPI3"], t["SPI3_category"], idx._BAND_COLORS,
            f"Drought status (SPI-3) -- {place}, {bl} baseline", "SPI-3 (0 = normal; more negative = drier)",
            baseline_note, fig_dir / "spi_timeseries.png",
            subtitle="3-month rainfall total expressed as a standardized deviation from the calendar-month "
                     "average. Negative values indicate below-normal rainfall; magnitude indicates severity.",
            formula=F["SPI"])
        idx.plot_index_small_multiples(t, "SPI", idx.SPI_SCALES,
            f"SPI (Standardized Precipitation Index), all scales -- {place}, {bl} baseline",
            "SPI (standard deviations from normal)", baseline_note, fig_dir / "spi_timeseries_detailed.png",
            subtitle="SPI computed at four accumulation periods (1/3/6/12 months).", formula=F["SPI"])

    if "pni" in wanted_tp_only:
        print(f"[{place}] Computing PNI...")
        t = pd.DataFrame({"precip_mm": tp_monthly, "PNI_pct": idx.compute_pni(tp_monthly)})
        t["PNI_category"] = t["PNI_pct"].apply(lambda v: idx._categorize(v, idx.PNI_CATEGORIES))
        tables["PNI"] = t
        idx.plot_simple_category_bars(t["PNI_pct"], t["PNI_category"], idx._PNI_BAND_COLORS,
            f"Rainfall as % of normal (PNI) -- {place}, {bl} baseline", "% of normal monthly rain",
            baseline_note, fig_dir / "pni_timeseries.png",
            subtitle="Monthly rainfall as a percentage of the average for that calendar month.", formula=F["PNI"])

    if "imd-departure" in wanted_tp_only:
        print(f"[{place}] Computing IMD-style percentage departure...")
        t = pd.DataFrame({"precip_mm": tp_monthly, "departure_pct": idx.compute_imd_departure(tp_monthly)})
        t["departure_category"] = t["departure_pct"].apply(lambda v: idx._categorize(v, idx.IMD_DEPARTURE_CATEGORIES))
        tables["IMD_Departure"] = t
        idx.plot_simple_category_bars(t["departure_pct"], t["departure_category"], idx._IMD_BAND_COLORS,
            f"Rainfall departure from normal, IMD convention -- {place}, {bl} baseline",
            "% departure from normal (0 = normal)", baseline_note, fig_dir / "imd_departure_timeseries.png",
            subtitle="Monthly rainfall departure using India Meteorological Department's own category "
                     "definitions (Excess/Normal/Deficient/Scanty/No rain).",
            formula=F["IMD"], clip_display=(-100, 150))

    if "weekly" in wanted_tp_only:
        print(f"[{place}] Computing weekly SPI + IMD-departure...")
        tp_weekly = idx.resample_weekly(tp_daily)
        week_key = idx.iso_week_key(tp_weekly)
        t = pd.DataFrame({"precip_mm": tp_weekly})
        t["SPI1"] = idx.compute_spi_generic(tp_weekly, week_key)
        t["SPI1_category"] = t["SPI1"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
        t["departure_pct"] = idx.compute_imd_departure(tp_weekly, week_key)
        t["departure_category"] = t["departure_pct"].apply(lambda v: idx._categorize(v, idx.IMD_DEPARTURE_CATEGORIES))
        tables["Weekly"] = t
        idx.plot_simple_category_bars(t["SPI1"], t["SPI1_category"], idx._BAND_COLORS,
            f"Drought status, weekly (SPI, 1-week) -- {place}, {bl} baseline", "SPI (0 = normal; more negative = drier)",
            baseline_note, fig_dir / "spi_weekly_timeseries.png", bar_width=5,
            subtitle="Each bar is one ISO calendar week's rainfall, standardized against that same week-of-year.",
            formula=F["SPI"])
        idx.plot_simple_category_bars(t["departure_pct"], t["departure_category"], idx._IMD_BAND_COLORS,
            f"Rainfall departure, weekly, IMD convention -- {place}, {bl} baseline",
            "% departure from normal (0 = normal)", baseline_note, fig_dir / "imd_departure_weekly_timeseries.png",
            bar_width=5, subtitle="Weekly rainfall departure from that ISO week's average.",
            formula=F["IMD"], clip_display=(-100, 150))

    if "fortnightly" in wanted_tp_only:
        print(f"[{place}] Computing fortnightly SPI + IMD-departure...")
        tp_fn = idx.resample_fortnightly(tp_daily)
        fn_key = idx.fortnight_key(tp_fn)
        t = pd.DataFrame({"precip_mm": tp_fn})
        t["SPI1"] = idx.compute_spi_generic(tp_fn, fn_key)
        t["SPI1_category"] = t["SPI1"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
        t["departure_pct"] = idx.compute_imd_departure(tp_fn, fn_key)
        t["departure_category"] = t["departure_pct"].apply(lambda v: idx._categorize(v, idx.IMD_DEPARTURE_CATEGORIES))
        tables["Fortnightly"] = t
        idx.plot_simple_category_bars(t["SPI1"], t["SPI1_category"], idx._BAND_COLORS,
            f"Drought status, fortnightly (SPI) -- {place}, {bl} baseline", "SPI (0 = normal; more negative = drier)",
            baseline_note, fig_dir / "spi_fortnightly_timeseries.png", bar_width=10,
            subtitle="Each bar is one 14-day period's rainfall, standardized against that same period-of-year.",
            formula=F["SPI"])
        idx.plot_simple_category_bars(t["departure_pct"], t["departure_category"], idx._IMD_BAND_COLORS,
            f"Rainfall departure, fortnightly, IMD convention -- {place}, {bl} baseline",
            "% departure from normal (0 = normal)", baseline_note, fig_dir / "imd_departure_fortnightly_timeseries.png",
            bar_width=10, subtitle="Fortnightly rainfall departure from that 14-day period's average.",
            formula=F["IMD"], clip_display=(-100, 150))

    if "rai" in wanted_tp_only:
        print(f"[{place}] Computing RAI...")
        t = pd.DataFrame({"precip_mm": tp_monthly, "RAI": idx.compute_rai(tp_monthly)})
        t["RAI_category"] = t["RAI"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
        tables["RAI"] = t
        idx.plot_simple_category_bars(t["RAI"], t["RAI_category"], idx._BAND_COLORS,
            f"Drought status, cross-check (RAI) -- {place}, {bl} baseline", "RAI (0 = normal; more negative = drier)",
            baseline_note, fig_dir / "rai_timeseries.png",
            subtitle="Monthly rainfall anomaly rescaled by the mean of the ten wettest/driest years on record "
                     "(Van Rooy, 1965).", formula=F["RAI"])

    if "pci" in wanted_tp_only:
        print(f"[{place}] Computing PCI...")
        t = pd.DataFrame({"PCI": idx.compute_pci(tp_monthly)})
        t["PCI_category"] = t["PCI"].apply(lambda v: idx._categorize(v, idx.PCI_CATEGORIES))
        tables["PCI"] = t
        idx.plot_simple_category_bars(t["PCI"], t["PCI_category"], idx._PCI_BAND_COLORS,
            f"Rainfall concentration through the year (PCI) -- {place}, {bl}", "PCI", baseline_note,
            fig_dir / "pci_timeseries.png", bar_width=200,
            subtitle="How concentrated the year's rainfall is into few months vs. spread evenly.", formula=F["PCI"])

    if "spei" in wanted_pet:
        print(f"[{place}] Computing SPEI...")
        t = pd.DataFrame({"precip_mm_1mo": tp_monthly, "pet_mm_1mo": pet_monthly,
                           "water_balance_mm_1mo": water_balance_monthly})
        for scale in idx.SPI_SCALES:
            t[f"water_balance_mm_{scale}mo"] = water_balance_monthly.rolling(scale, min_periods=scale).sum()
            t[f"SPEI{scale}"] = idx.compute_spei_zscore(water_balance_monthly, scale)
            t[f"SPEI{scale}_category"] = t[f"SPEI{scale}"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
        tables["SPEI"] = t
        idx.plot_simple_category_bars(t["SPEI3"], t["SPEI3_category"], idx._BAND_COLORS,
            f"Drought status incl. heat stress (SPEI-3) -- {place}, {bl} baseline",
            "SPEI-3 (0 = normal; more negative = drier)", baseline_note, fig_dir / "spei_timeseries.png",
            subtitle="Standardized deviation of the 3-month water balance (rainfall minus PET).", formula=F["SPEI"])
        idx.plot_index_small_multiples(t, "SPEI", idx.SPI_SCALES,
            f"SPEI, all scales -- {place}, {bl} baseline", "SPEI (z-score, demo method)", baseline_note,
            fig_dir / "spei_timeseries_detailed.png", formula=F["SPEI"])

    if "eddi" in wanted_pet:
        print(f"[{place}] Computing EDDI...")
        t = pd.DataFrame({"pet_mm_1mo": pet_monthly})
        for scale in idx.SPI_SCALES:
            t[f"pet_mm_{scale}mo"] = pet_monthly.rolling(scale, min_periods=scale).sum()
            t[f"EDDI{scale}"] = idx.compute_eddi(pet_monthly, scale)
            t[f"EDDI{scale}_category"] = (-t[f"EDDI{scale}"]).apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
        tables["EDDI"] = t
        idx.plot_simple_category_bars(t["EDDI3"], t["EDDI3_category"], idx._BAND_COLORS,
            f"Evaporative demand drought status (EDDI-3) -- {place}, {bl}", "EDDI-3 (HIGH = more stress)",
            baseline_note, fig_dir / "eddi_timeseries.png",
            subtitle="Standardized evaporative demand. Sign convention reversed vs SPI/SPEI: positive = drought.",
            formula=F["EDDI"])
        idx.plot_index_small_multiples(t, "EDDI", idx.SPI_SCALES,
            f"EDDI, all scales -- {place}, {bl}", "EDDI (HIGH = more stress)", baseline_note,
            fig_dir / "eddi_timeseries_detailed.png", formula=F["EDDI"])

    if "ai" in wanted_pet:
        print(f"[{place}] Computing AI (Aridity Index)...")
        t = pd.DataFrame({"AI": idx.compute_aridity_index(tp_monthly, pet_monthly)})
        t["AI_category"] = t["AI"].apply(lambda v: idx._categorize(v, idx.AI_CATEGORIES))
        tables["AI"] = t
        idx.plot_simple_category_bars(t["AI"], t["AI_category"], idx._AI_BAND_COLORS,
            f"Aridity Index (annual P / PET) -- {place}, {bl}", "AI = Annual rainfall / Annual PET",
            baseline_note, fig_dir / "ai_timeseries.png", bar_width=200,
            subtitle="Ratio of annual rainfall to annual PET. Below 0.65 = semi-arid (UNEP).", formula=F["AI"])

    if "water-balance" in wanted_pet:
        print(f"[{place}] Computing Water Balance Anomaly...")
        wb_anomaly = idx.compute_water_balance_anomaly(water_balance_monthly)
        t = pd.DataFrame({"water_balance_mm": water_balance_monthly, "water_balance_anomaly_mm": wb_anomaly})
        t["anomaly_category"] = t["water_balance_anomaly_mm"].apply(lambda v: idx._categorize(v / 50.0, idx.DROUGHT_CATEGORIES))
        tables["WaterBalanceAnomaly"] = t
        idx.plot_simple_category_bars(t["water_balance_anomaly_mm"], t["anomaly_category"], idx._BAND_COLORS,
            f"Water balance (Rain-PET) anomaly -- {place}, {bl}", "mm vs normal", baseline_note,
            fig_dir / "water_balance_anomaly_timeseries.png",
            subtitle="Monthly water balance (rainfall minus PET) deviation from its average.", formula=F["WB"])

    if have_t2m:
        anomaly_files = sorted(anomalies_dir.glob("t2m/t2m_*_anomaly.nc"))
        if anomaly_files:
            anomaly_ds = xr.open_mfdataset([str(f) for f in anomaly_files], combine="by_coords")
            t2m_mean_anomaly_daily = idx._area_mean(anomaly_ds["t2m_mean_anomaly"]).to_series().resample("MS").mean()
            t = pd.DataFrame({"t2m_mean_anomaly": t2m_mean_anomaly_daily})
            t["t2m_category"] = t["t2m_mean_anomaly"].apply(lambda v: idx._categorize(v, idx.TEMP_CATEGORIES))
            tables["Temperature"] = t
            idx.plot_simple_category_bars(t["t2m_mean_anomaly"], t["t2m_category"], idx._TEMP_BAND_COLORS,
                f"Temperature vs normal -- {place}, {bl} baseline", "degC vs normal", baseline_note,
                fig_dir / "temperature_anomaly_timeseries.png",
                subtitle="Monthly mean temperature deviation (degC) from its calendar-day normal.", formula=F["Temp"])

    if tables:
        indices_file = indices_dir / f"{place}_drought_indices_{start_year}_{end_year}.xlsx"
        with pd.ExcelWriter(indices_file) as writer:
            for sheet, t in tables.items():
                t.to_excel(writer, sheet_name=sheet)
            pd.DataFrame({"note": [baseline_note]}).to_excel(writer, sheet_name="README", index=False)
        print(f"  Wrote {indices_file}")

    if "etccdi" in wanted_tp_only:
        print(f"[{place}] Computing annual extreme indices (CDD/CWD/R95p/SDII)...")
        pl.compute_all_indices(["cdd", "cwd", "r95p", "sdii"], start_year, end_year, processed_dir,
                                annual_indices_dir, baseline_start, baseline_end, defaults=defaults, region_name=place)
        etccdi_table = pd.DataFrame()
        etccdi_meta = {
            "cdd": ("Longest dry spell (CDD)", "days", "Max consecutive days with rainfall below 1mm (ETCCDI)."),
            "cwd": ("Longest wet spell (CWD)", "days", "Max consecutive days with rainfall of 1mm or more (ETCCDI)."),
            "r95p": ("Heavy-rain contribution (R95p)", "mm", "Annual rainfall from days exceeding the 95th "
                     "percentile of wet-day rainfall (ETCCDI)."),
            "sdii": ("Mean wet-day rain intensity (SDII)", "mm/day", "Mean rainfall per wet day (ETCCDI)."),
        }
        etccdi_formula_key = {"cdd": "CDD", "cwd": "CWD", "r95p": "R95p", "sdii": "SDII"}
        for name in etccdi_meta:
            files = sorted(annual_indices_dir.glob(f"{name}_*.nc"))
            if not files:
                continue
            ds = xr.open_mfdataset([str(f) for f in files], combine="by_coords")
            etccdi_table[name] = idx._area_mean(ds[name]).to_series()
        if not etccdi_table.empty:
            etccdi_file = indices_dir / f"{place}_annual_extremes_{start_year}_{end_year}.xlsx"
            etccdi_table.to_excel(etccdi_file, sheet_name="Annual_extremes")
            print(f"  Wrote {etccdi_file}")
            for name, (label, unit, explainer) in etccdi_meta.items():
                if name not in etccdi_table.columns:
                    continue
                series = etccdi_table[name].dropna()
                idx.plot_simple_category_bars(series, pd.Series("", index=series.index), {"": pl.RAIN_COLOR},
                    f"{label} -- {place}, {bl} baseline", unit, baseline_note, fig_dir / f"{name}_timeseries.png",
                    annotate_extremes=True, bar_width=200, subtitle=explainer, formula=F[etccdi_formula_key[name]])

    if "spi" in wanted_tp_only or "imd-departure" in wanted_tp_only:
        print(f"[{place}] Drawing spatial maps (driest/wettest month)...")
        spi3 = tables.get("SPI", pd.DataFrame()).get("SPI3", pd.Series(dtype=float)).dropna()
        if len(spi3):
            for label, month_ts in [("driest", spi3.idxmin()), ("wettest", spi3.idxmax())]:
                y, m = month_ts.year, month_ts.month
                f = anomalies_dir / "tp" / f"tp_{y}_anomaly.nc"
                if f.exists():
                    ds = xr.open_dataset(f)
                    pct = ds["tp_sum_pct_of_normal"].sel(time=ds["time"].dt.month == m).mean(dim="time") - 100.0
                    idx.plot_spatial_anomaly_map(pct, f"Rainfall anomaly: {label} month on record ({y}-{m:02d}) -- {place}",
                        "% deviation from normal", region, fig_dir / f"spatial_precip_anomaly_{label}_{y}_{m:02d}.png",
                        hot_is_red=False,
                        subtitle="Grid-cell rainfall deviation from the baseline normal for that month.")
    if "Temperature" in tables:
        t2m_valid = tables["Temperature"]["t2m_mean_anomaly"].dropna()
        if len(t2m_valid):
            hot_month = t2m_valid.idxmax()
            y, m = hot_month.year, hot_month.month
            f = anomalies_dir / "t2m" / f"t2m_{y}_anomaly.nc"
            if f.exists():
                ds = xr.open_dataset(f)
                spatial = ds["t2m_mean_anomaly"].sel(time=ds["time"].dt.month == m).mean(dim="time")
                idx.plot_spatial_anomaly_map(spatial, f"Temperature anomaly: hottest month on record ({y}-{m:02d}) -- {place}",
                    "degC deviation from normal", region, fig_dir / f"spatial_temp_anomaly_{y}_{m:02d}.png",
                    hot_is_red=True,
                    subtitle="Grid-cell temperature deviation (degC) from the baseline normal for that month.")

    print(f"\nDone. Outputs in: {out_dir}")
    print(" ", baseline_note)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--place", required=True, help="Region name, used in titles/filenames (e.g. 'Chhindwara').")
    loc = p.add_mutually_exclusive_group(required=True)
    loc.add_argument("--bbox", type=float, nargs=4, metavar=("NORTH", "WEST", "SOUTH", "EAST"),
                      help="Bounding box in degrees.")
    loc.add_argument("--boundary-file", type=Path, help="A KML/shapefile/GeoJSON boundary to clip to.")
    p.add_argument("--start-year", type=int, required=True)
    p.add_argument("--end-year", type=int, required=True)
    p.add_argument("--baseline-start", type=int, help="Defaults to --start-year.")
    p.add_argument("--baseline-end", type=int, help="Defaults to --end-year.")
    p.add_argument("--variables", default="t2m,tp",
                    help="Comma-separated ERA5-Land short names to download (default: t2m,tp). "
                         "Add d2m,u10,v10,ssrd,sp to enable full Penman-Monteith PET.")
    p.add_argument("--indices", default="all",
                    help=f"Comma-separated indices to compute, or 'all'. Choices: {','.join(ALL_INDICES)} "
                         f"(default: all -- each still gated on whether its required variables were downloaded).")
    p.add_argument("--raw-dir", type=Path, help="Defaults to data/<place>/raw.")
    p.add_argument("--out-dir", type=Path, help="Defaults to data/<place>.")
    p.add_argument("--parallel", type=int, default=4, help="Concurrent CDS requests (default 4).")
    p.add_argument("--skip-download", action="store_true",
                    help="Only rebuild normals/anomalies/indices/figures from data already on disk.")
    args = p.parse_args()

    root = Path(__file__).resolve().parent
    out_dir = args.out_dir or (root / "data" / args.place)
    raw_dir = args.raw_dir or (out_dir / "raw")
    variables = [v.strip() for v in args.variables.split(",") if v.strip()]
    requested_indices = ALL_INDICES if args.indices.strip().lower() == "all" else \
        [i.strip() for i in args.indices.split(",") if i.strip()]
    unknown = set(requested_indices) - set(ALL_INDICES)
    if unknown:
        p.error(f"Unknown --indices entries {sorted(unknown)}; choices are {ALL_INDICES}")
    baseline_start = args.baseline_start or args.start_year
    baseline_end = args.baseline_end or args.end_year

    region = pl.resolve_region(bbox=tuple(args.bbox) if args.bbox else None,
                                boundary_file=args.boundary_file, name=args.place, buffer_deg=0.0)

    t0 = time.time()
    run(args.place, region, variables, requested_indices, args.start_year, args.end_year,
        baseline_start, baseline_end, raw_dir, out_dir, args.parallel, args.skip_download)
    print(f"[{args.place}] Total time: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
