"""Regenerate all Chhindwara figures in the government-report style (official=True)
into Chhindwara_Final_Results_1991_2026/figures_government/.

Reads from the already-computed Excel workbooks and anomaly NetCDFs -- no need
to re-run the full download/preprocess/index pipeline, since none of that
output changes; only the figure styling does.

Run: python regenerate_government_figures.py
"""
import sys
from pathlib import Path

import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as pl
import indices as idx

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "chhindwara_indices"
ANOMALIES_DIR = OUT / "anomalies"
INDICES_DIR = OUT / "drought_indices"
FIG_DIR = ROOT / "Chhindwara_Final_Results_1991_2026" / "figures_government"
BOUNDARY_FILE = ROOT / "Chhindwara.kml"

BASELINE_START, BASELINE_END, DATA_END = 1991, 2020, 2026
DISTRICT = "Chhindwara"


def main() -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    region = pl.resolve_region(bbox=None, boundary_file=BOUNDARY_FILE, name="chhindwara", buffer_deg=0.0)

    xl = pd.ExcelFile(INDICES_DIR / f"chhindwara_drought_indices_{BASELINE_START}_{DATA_END}.xlsx")
    spi_table = xl.parse("SPI", index_col=0, parse_dates=True)
    spei_table = xl.parse("SPEI", index_col=0, parse_dates=True)
    pni_table = xl.parse("PNI", index_col=0, parse_dates=True)
    imd_table = xl.parse("IMD_Departure", index_col=0, parse_dates=True)
    weekly_table = xl.parse("Weekly", index_col=0, parse_dates=True)
    fortnightly_table = xl.parse("Fortnightly", index_col=0, parse_dates=True)
    rai_table = xl.parse("RAI", index_col=0, parse_dates=True)
    pci_table = xl.parse("PCI", index_col=0, parse_dates=True)
    ai_table = xl.parse("AI", index_col=0, parse_dates=True)
    eddi_table = xl.parse("EDDI", index_col=0, parse_dates=True)
    wb_table = xl.parse("WaterBalanceAnomaly", index_col=0, parse_dates=True)

    anomaly_files = sorted(ANOMALIES_DIR.glob("t2m/t2m_*_anomaly.nc"))
    anomaly_ds = xr.open_mfdataset([str(f) for f in anomaly_files], combine="by_coords")
    t2m_mean_anomaly_daily = idx._area_mean(anomaly_ds["t2m_mean_anomaly"]).to_series().resample("MS").mean()
    temp_table = pd.DataFrame({"t2m_mean_anomaly": t2m_mean_anomaly_daily})
    temp_table["t2m_category"] = temp_table["t2m_mean_anomaly"].apply(lambda v: idx._categorize(v, idx.TEMP_CATEGORIES))

    etccdi_table = pd.DataFrame()
    etccdi_file = INDICES_DIR / f"chhindwara_annual_extremes_{BASELINE_START}_{DATA_END}.xlsx"
    if etccdi_file.exists():
        etccdi_table = pd.read_excel(etccdi_file, index_col=0, parse_dates=True, sheet_name="Annual_extremes")

    baseline_note = (f"Data: {BASELINE_START}-{DATA_END} (the final year may be partial). Normal/baseline: full WMO "
                      f"30-year climate normal period {BASELINE_START}-{BASELINE_END}.")
    bl = f"{BASELINE_START}-{BASELINE_END}"
    F = idx.FORMULAS
    kw = dict(official=True, district=DISTRICT)

    print("Rendering government-style figures...")
    idx.plot_simple_category_bars(spi_table["SPI3"], spi_table["SPI3_category"], idx._BAND_COLORS,
        f"Drought Status (SPI-3) -- {DISTRICT}, {bl} Baseline", "SPI-3 (0 = normal; more negative = drier)",
        baseline_note, FIG_DIR / "spi_timeseries.png",
        subtitle="3-month rainfall total expressed as a standardized deviation from the 1991-2020 calendar-month "
                 "average. Negative values indicate below-normal rainfall; magnitude indicates severity.",
        formula=F["SPI"], **kw)
    idx.plot_simple_category_bars(spei_table["SPEI3"], spei_table["SPEI3_category"], idx._BAND_COLORS,
        f"Drought Status incl. Heat Stress (SPEI-3) -- {DISTRICT}, {bl} Baseline",
        "SPEI-3 (0 = normal; more negative = drier)", baseline_note, FIG_DIR / "spei_timeseries.png",
        subtitle="Standardized deviation of the 3-month water balance (rainfall minus potential evapotranspiration) "
                 "from its 1991-2020 average.", formula=F["SPEI"], **kw)
    idx.plot_simple_category_bars(pni_table["PNI_pct"], pni_table["PNI_category"], idx._PNI_BAND_COLORS,
        f"Rainfall as % of Normal (PNI) -- {DISTRICT}, {bl} Baseline", "% of normal monthly rain",
        baseline_note, FIG_DIR / "pni_timeseries.png",
        subtitle="Monthly rainfall expressed as a percentage of the 1991-2020 average for that calendar month.",
        formula=F["PNI"], **kw)
    idx.plot_simple_category_bars(imd_table["departure_pct"], imd_table["departure_category"], idx._IMD_BAND_COLORS,
        f"Rainfall Departure from Normal, IMD Convention -- {DISTRICT}, {bl} Baseline",
        "% departure from normal (0 = normal)", baseline_note, FIG_DIR / "imd_departure_timeseries.png",
        subtitle="Monthly rainfall departure using India Meteorological Department's own category definitions "
                 "(Excess/Normal/Deficient/Scanty/No rain).", formula=F["IMD"], clip_display=(-100, 150), **kw)
    idx.plot_simple_category_bars(weekly_table["SPI1"], weekly_table["SPI1_category"], idx._BAND_COLORS,
        f"Drought Status, Weekly (SPI) -- {DISTRICT}, {bl} Baseline", "SPI (0 = normal; more negative = drier)",
        baseline_note, FIG_DIR / "spi_weekly_timeseries.png", bar_width=5,
        subtitle="Each bar is one ISO calendar week's rainfall, standardized against that same week-of-year.",
        formula=F["SPI"], **kw)
    idx.plot_simple_category_bars(weekly_table["departure_pct"], weekly_table["departure_category"], idx._IMD_BAND_COLORS,
        f"Rainfall Departure, Weekly, IMD Convention -- {DISTRICT}, {bl} Baseline",
        "% departure from normal (0 = normal)", baseline_note, FIG_DIR / "imd_departure_weekly_timeseries.png",
        bar_width=5, subtitle="Weekly rainfall departure from that ISO week's average.",
        formula=F["IMD"], clip_display=(-100, 150), **kw)
    idx.plot_simple_category_bars(fortnightly_table["SPI1"], fortnightly_table["SPI1_category"], idx._BAND_COLORS,
        f"Drought Status, Fortnightly (SPI) -- {DISTRICT}, {bl} Baseline", "SPI (0 = normal; more negative = drier)",
        baseline_note, FIG_DIR / "spi_fortnightly_timeseries.png", bar_width=10,
        subtitle="Each bar is one 14-day period's rainfall, standardized against that same period-of-year.",
        formula=F["SPI"], **kw)
    idx.plot_simple_category_bars(fortnightly_table["departure_pct"], fortnightly_table["departure_category"], idx._IMD_BAND_COLORS,
        f"Rainfall Departure, Fortnightly, IMD Convention -- {DISTRICT}, {bl} Baseline",
        "% departure from normal (0 = normal)", baseline_note, FIG_DIR / "imd_departure_fortnightly_timeseries.png",
        bar_width=10, subtitle="Fortnightly rainfall departure from that 14-day period's average.",
        formula=F["IMD"], clip_display=(-100, 150), **kw)
    idx.plot_simple_category_bars(rai_table["RAI"], rai_table["RAI_category"], idx._BAND_COLORS,
        f"Drought Status, Cross-check (RAI) -- {DISTRICT}, {bl} Baseline", "RAI (0 = normal; more negative = drier)",
        baseline_note, FIG_DIR / "rai_timeseries.png",
        subtitle="Monthly rainfall anomaly rescaled by the mean of the ten wettest and ten driest years on record "
                 "(Van Rooy, 1965).", formula=F["RAI"], **kw)
    idx.plot_simple_category_bars(temp_table["t2m_mean_anomaly"], temp_table["t2m_category"], idx._TEMP_BAND_COLORS,
        f"Temperature vs Normal -- {DISTRICT}, {bl} Baseline", "degC vs normal", baseline_note,
        FIG_DIR / "temperature_anomaly_timeseries.png",
        subtitle="Monthly mean temperature expressed as a deviation (degC) from its 1991-2020 calendar-day normal.",
        formula=F["Temp"], **kw)
    idx.plot_simple_category_bars(pci_table["PCI"], pci_table["PCI_category"], idx._PCI_BAND_COLORS,
        f"Rainfall Concentration Through the Year (PCI) -- {DISTRICT}, {bl}", "PCI", baseline_note,
        FIG_DIR / "pci_timeseries.png", bar_width=200,
        subtitle="Annual distribution of rainfall across the 12 calendar months. Higher values indicate rainfall "
                 "concentrated in fewer months.", formula=F["PCI"], **kw)
    idx.plot_simple_category_bars(ai_table["AI"], ai_table["AI_category"], idx._AI_BAND_COLORS,
        f"Aridity Index (Annual P / PET) -- {DISTRICT}, {bl}", "AI = Annual rainfall / Annual PET", baseline_note,
        FIG_DIR / "ai_timeseries.png", bar_width=200,
        subtitle="Ratio of annual rainfall to annual potential evapotranspiration. Below 0.65 indicates "
                 "semi-arid conditions (UNEP classification).", formula=F["AI"], **kw)
    idx.plot_simple_category_bars(eddi_table["EDDI3"], eddi_table["EDDI3_category"], idx._BAND_COLORS,
        f"Evaporative Demand Drought Status (EDDI-3) -- {DISTRICT}, {bl}", "EDDI-3 (HIGH = more evaporative stress)",
        baseline_note, FIG_DIR / "eddi_timeseries.png",
        subtitle="Standardized evaporative demand, independent of rainfall. Sign convention reversed relative to "
                 "the other indices: positive values indicate drought.", formula=F["EDDI"], **kw)
    idx.plot_simple_category_bars(wb_table["water_balance_anomaly_mm"], wb_table["anomaly_category"], idx._BAND_COLORS,
        f"Water Balance (Rain-PET) Anomaly -- {DISTRICT}, {bl}", "mm vs normal", baseline_note,
        FIG_DIR / "water_balance_anomaly_timeseries.png",
        subtitle="Monthly water balance (rainfall minus potential evapotranspiration) deviation from its "
                 "1991-2020 average.", formula=F["WB"], **kw)

    idx.plot_index_small_multiples(spi_table, "SPI", idx.SPI_SCALES,
        f"SPI (Standardized Precipitation Index), All Scales -- {DISTRICT}, {bl} Baseline",
        "SPI (standard deviations from normal)", baseline_note, FIG_DIR / "spi_timeseries_detailed.png",
        subtitle="SPI computed at four accumulation periods (1/3/6/12 months).", formula=F["SPI"], **kw)
    idx.plot_index_small_multiples(spei_table, "SPEI", idx.SPI_SCALES,
        f"SPEI (Standardized Precip-Evapotranspiration Index), All Scales -- {DISTRICT}, {bl} Baseline",
        "SPEI (z-score, demo method)", baseline_note, FIG_DIR / "spei_timeseries_detailed.png",
        subtitle="SPEI computed at four accumulation periods (1/3/6/12 months).", formula=F["SPEI"], **kw)
    idx.plot_index_small_multiples(eddi_table, "EDDI", idx.SPI_SCALES,
        f"EDDI (Evaporative Demand Drought Index), All Scales -- {DISTRICT}, {bl} Baseline",
        "EDDI (HIGH = more evaporative stress)", baseline_note, FIG_DIR / "eddi_timeseries_detailed.png",
        subtitle="EDDI computed at four accumulation periods.", formula=F["EDDI"], **kw)

    if not etccdi_table.empty:
        etccdi_meta = {
            "cdd": ("Longest Dry Spell (CDD)", "days", "Maximum number of consecutive days with daily rainfall "
                    "below 1mm (ETCCDI definition)."),
            "cwd": ("Longest Wet Spell (CWD)", "days", "Maximum number of consecutive days with daily rainfall "
                    "of 1mm or more (ETCCDI definition)."),
            "r95p": ("Heavy-Rain Contribution (R95p)", "mm", "Total annual rainfall contributed by days "
                     "exceeding the 95th percentile of wet-day rainfall (ETCCDI definition)."),
            "sdii": ("Mean Wet-Day Rain Intensity (SDII)", "mm/day", "Mean rainfall per wet day (ETCCDI definition)."),
        }
        etccdi_formula_key = {"cdd": "CDD", "cwd": "CWD", "r95p": "R95p", "sdii": "SDII"}
        for name, (label, unit, explainer) in etccdi_meta.items():
            if name not in etccdi_table.columns:
                continue
            series = etccdi_table[name].dropna()
            idx.plot_simple_category_bars(series, pd.Series("", index=series.index), {"": pl.RAIN_COLOR},
                f"{label} -- {DISTRICT}, {bl} Baseline", unit, baseline_note, FIG_DIR / f"{name}_timeseries.png",
                annotate_extremes=True, bar_width=200, subtitle=explainer, formula=F[etccdi_formula_key[name]], **kw)

    print("Drawing spatial maps...")
    spi3 = spi_table["SPI3"].dropna()
    if len(spi3):
        for label, month_ts in [("driest", spi3.idxmin()), ("wettest", spi3.idxmax())]:
            y, m = month_ts.year, month_ts.month
            f = ANOMALIES_DIR / "tp" / f"tp_{y}_anomaly.nc"
            if f.exists():
                ds = xr.open_dataset(f)
                pct = ds["tp_sum_pct_of_normal"].sel(time=ds["time"].dt.month == m).mean(dim="time") - 100.0
                idx.plot_spatial_anomaly_map(
                    pct, f"Rainfall Anomaly: {label.capitalize()} Month on Record ({y}-{m:02d}) -- {DISTRICT} District",
                    "% deviation from the 1991-2020 normal", region, FIG_DIR / f"spatial_precip_anomaly_{label}_{y}_{m:02d}.png",
                    hot_is_red=False, baseline_note=baseline_note,
                    subtitle="Grid-cell rainfall deviation from the 1991-2020 normal for that month.", **kw)

    hot_month = t2m_mean_anomaly_daily.idxmax() if len(t2m_mean_anomaly_daily.dropna()) else None
    if hot_month is not None:
        y, m = hot_month.year, hot_month.month
        f = ANOMALIES_DIR / "t2m" / f"t2m_{y}_anomaly.nc"
        if f.exists():
            ds = xr.open_dataset(f)
            spatial = ds["t2m_mean_anomaly"].sel(time=ds["time"].dt.month == m).mean(dim="time")
            idx.plot_spatial_anomaly_map(spatial, f"Temperature Anomaly: Hottest Month on Record ({y}-{m:02d}) -- {DISTRICT} District",
                "degC deviation from the 1991-2020 normal", region, FIG_DIR / f"spatial_temp_anomaly_{y}_{m:02d}.png",
                hot_is_red=True, baseline_note=baseline_note,
                subtitle="Grid-cell temperature deviation (degC) from the 1991-2020 normal for that month.", **kw)

    print(f"\nDone. Government-style figures in: {FIG_DIR}")


if __name__ == "__main__":
    main()
