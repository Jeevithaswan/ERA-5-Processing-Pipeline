"""Chhindwara 30-year normals, anomalies, and drought indices -- single source.

Everything here comes from ONE dataset: our own ERA5-Land hourly download
(backtest/chhindwara_raw/{t2m,tp}/), not a mix with Abishree ma'am's Excel.
That Excel has no rainfall column, so it can't drive drought indices anyway --
blending it in would just stitch two differently-sourced grids together for
no benefit. Reusing pipeline.py's existing, tested WMO normals/anomalies code
keeps this consistent with the rest of the project.

Baseline: t2m and tp are both fully, continuously downloaded for the full
WMO 1991-2020 climate normal period (30 years). This is the final run on
the complete dataset (no longer interim).

All index math and figure rendering lives in indices.py (place-agnostic,
shared with run_climatology.py's generalized any-place CLI) -- this script
is just the Chhindwara-specific orchestration: its paths, dates, and which
charts to draw in which order.

Indices computed:
  - Temperature normals + daily anomalies (pipeline.py's existing stage).
  - SPI (Standardized Precipitation Index), gamma-fit per calendar month,
    scales 1/3/6/12 months -- the standard drought index.
  - SPEI (Standardized Precip-Evapotranspiration Index), using FAO-56
    Penman-Monteith reference ET0 (needs t2m, tp, d2m, u10/v10, ssrd, sp --
    all seven now downloaded through DATA_END). Demo-grade: standardized via
    a per-calendar-month z-score of the water balance (P-PET), not the full
    3-parameter log-logistic fit the published SPEI method uses -- flagged
    as such in the output; refine later if this becomes more than a demo.
  - PNI (Percent of Normal precipitation Index) -- simple cross-check.

Run: python chhindwara_indices.py
"""
import sys
from pathlib import Path

import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as pl
import indices as idx

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "backtest" / "chhindwara_raw"
BOUNDARY_FILE = ROOT / "Chhindwara.kml"

OUT = ROOT / "chhindwara_indices"
PROCESSED_DIR = OUT / "processed"
NORMALS_DIR = OUT / "normals"
ANOMALIES_DIR = OUT / "anomalies"
INDICES_DIR = OUT / "drought_indices"
ANNUAL_INDICES_DIR = OUT / "annual_indices"  # pipeline.py's xclim-based ETCCDI indices (cdd/cwd/r95p/sdii)
FIG_DIR = OUT / "figures"

BASELINE_START, BASELINE_END = 1991, 2020  # full WMO 30-year climate normal period
DATA_END = 2026  # data/anomalies extend through here; the NORMAL stays fixed to BASELINE_END above.
                 # tp/t2m raw data covers through 2026-07 (ERA5-Land's own publication lag means
                 # later 2026 months aren't out yet) -- partial final year is fine, monthly resampling
                 # just yields fewer rows for it, not an error.
CHHINDWARA_LAT = 22.0  # centroid; only needed by the Hargreaves PET fallback


# =============================================================================
# Step 1: preprocess raw ERA5-Land -> daily, then WMO normals + anomalies
# (reuses pipeline.py's existing, tested stage 2/3/4 code directly)
# =============================================================================

def preprocess_and_normals() -> pl.Region:
    defaults = pl.load_defaults()
    region = pl.resolve_region(bbox=None, boundary_file=BOUNDARY_FILE, name="chhindwara", buffer_deg=0.0)

    # Data range extends through DATA_END (current date), but the WMO normal
    # is still fit ONLY on BASELINE_START-BASELINE_END (1991-2020) -- pipeline.py
    # already supports this split (data range vs. baseline range as separate
    # arguments); this script just wasn't using that flexibility until now.
    for short in ["t2m", "tp"]:
        for year in range(BASELINE_START, DATA_END + 1):
            pl.process_variable_year(defaults, short, year, RAW_DIR, PROCESSED_DIR, region)

    pl.compute_normals(["t2m", "tp"], PROCESSED_DIR, NORMALS_DIR, BASELINE_START, BASELINE_END,
                        smoothing_window_days=5, defaults=defaults, region_name="chhindwara")
    pl.compute_all_anomalies(["t2m", "tp"], BASELINE_START, DATA_END, PROCESSED_DIR, NORMALS_DIR,
                              ANOMALIES_DIR, BASELINE_START, BASELINE_END, defaults=defaults,
                              region_name="chhindwara")
    return region


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    for d in (PROCESSED_DIR, NORMALS_DIR, ANOMALIES_DIR, INDICES_DIR, FIG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    print(f"Preprocessing + WMO normals/anomalies for t2m & tp, data {BASELINE_START}-{DATA_END}, "
          f"baseline {BASELINE_START}-{BASELINE_END}...")
    region = preprocess_and_normals()

    print("Building area-averaged monthly series...")
    tp_daily = idx.load_area_averaged_daily(PROCESSED_DIR, "tp", "tp_sum")
    tmax_daily = idx.load_area_averaged_daily(PROCESSED_DIR, "t2m", "t2m_max")
    tmin_daily = idx.load_area_averaged_daily(PROCESSED_DIR, "t2m", "t2m_min")
    tmean_daily = idx.load_area_averaged_daily(PROCESSED_DIR, "t2m", "t2m_mean")

    tp_monthly = tp_daily.resample("MS").sum()
    pet_daily = idx.penman_monteith_pet_daily(region, RAW_DIR, BASELINE_START, DATA_END)
    pet_monthly = pet_daily.resample("MS").sum()
    water_balance_monthly = tp_monthly - pet_monthly

    print("Computing SPI...")
    spi_table = pd.DataFrame({"precip_mm_1mo": tp_monthly})
    for scale in idx.SPI_SCALES:
        # The accumulated total actually used to compute this scale's SPI -- kept
        # alongside it explicitly, since "precip_mm" alone is ambiguous once there
        # are four different rolling windows in the same table.
        spi_table[f"precip_mm_{scale}mo"] = tp_monthly.rolling(scale, min_periods=scale).sum()
        spi_table[f"SPI{scale}"] = idx.compute_spi(tp_monthly, scale)
        spi_table[f"SPI{scale}_category"] = spi_table[f"SPI{scale}"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))

    print("Computing SPEI (FAO-56 Penman-Monteith PET, demo z-score standardization)...")
    spei_table = pd.DataFrame({"precip_mm_1mo": tp_monthly, "pet_mm_1mo": pet_monthly,
                                "water_balance_mm_1mo": water_balance_monthly})
    for scale in idx.SPI_SCALES:
        spei_table[f"water_balance_mm_{scale}mo"] = water_balance_monthly.rolling(scale, min_periods=scale).sum()
        spei_table[f"SPEI{scale}"] = idx.compute_spei_zscore(water_balance_monthly, scale)
        spei_table[f"SPEI{scale}_category"] = spei_table[f"SPEI{scale}"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))

    print("Computing PNI...")
    pni = idx.compute_pni(tp_monthly)
    pni_table = pd.DataFrame({"precip_mm": tp_monthly, "PNI_pct": pni})
    pni_table["PNI_category"] = pni_table["PNI_pct"].apply(lambda v: idx._categorize(v, idx.PNI_CATEGORIES))

    print("Computing IMD-style percentage departure...")
    imd_departure = idx.compute_imd_departure(tp_monthly)
    imd_table = pd.DataFrame({"precip_mm": tp_monthly, "departure_pct": imd_departure})
    imd_table["departure_category"] = imd_table["departure_pct"].apply(lambda v: idx._categorize(v, idx.IMD_DEPARTURE_CATEGORIES))

    print("Computing weekly and fortnightly SPI/IMD-departure...")
    tp_weekly = idx.resample_weekly(tp_daily)
    week_key = idx.iso_week_key(tp_weekly)
    weekly_table = pd.DataFrame({"precip_mm": tp_weekly})
    weekly_table["SPI1"] = idx.compute_spi_generic(tp_weekly, week_key)
    weekly_table["SPI1_category"] = weekly_table["SPI1"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
    weekly_table["departure_pct"] = idx.compute_imd_departure(tp_weekly, week_key)
    weekly_table["departure_category"] = weekly_table["departure_pct"].apply(lambda v: idx._categorize(v, idx.IMD_DEPARTURE_CATEGORIES))

    tp_fortnightly = idx.resample_fortnightly(tp_daily)
    fn_key = idx.fortnight_key(tp_fortnightly)
    fortnightly_table = pd.DataFrame({"precip_mm": tp_fortnightly})
    fortnightly_table["SPI1"] = idx.compute_spi_generic(tp_fortnightly, fn_key)
    fortnightly_table["SPI1_category"] = fortnightly_table["SPI1"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))
    fortnightly_table["departure_pct"] = idx.compute_imd_departure(tp_fortnightly, fn_key)
    fortnightly_table["departure_category"] = fortnightly_table["departure_pct"].apply(lambda v: idx._categorize(v, idx.IMD_DEPARTURE_CATEGORIES))

    print("Computing Decile Index (IMD method)...")
    decile = idx.compute_decile_index(tp_monthly)
    decile_table = pd.DataFrame({"precip_mm": tp_monthly, "decile": decile})
    decile_table["decile_category"] = decile_table["decile"].apply(idx._decile_category)

    print("Computing CZI...")
    czi_table = pd.DataFrame({"precip_mm_1mo": tp_monthly})
    for scale in idx.SPI_SCALES:
        czi_table[f"precip_mm_{scale}mo"] = tp_monthly.rolling(scale, min_periods=scale).sum()
        czi_table[f"CZI{scale}"] = idx.compute_czi(tp_monthly, scale)
        czi_table[f"CZI{scale}_category"] = czi_table[f"CZI{scale}"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))

    print("Computing RAI...")
    rai = idx.compute_rai(tp_monthly)
    rai_table = pd.DataFrame({"precip_mm": tp_monthly, "RAI": rai})
    rai_table["RAI_category"] = rai_table["RAI"].apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))

    print("Computing PCI...")
    pci = idx.compute_pci(tp_monthly)
    pci_table = pd.DataFrame({"PCI": pci})
    pci_table["PCI_category"] = pci_table["PCI"].apply(lambda v: idx._categorize(v, idx.PCI_CATEGORIES))

    print("Computing AI (Aridity Index)...")
    ai = idx.compute_aridity_index(tp_monthly, pet_monthly)
    ai_table = pd.DataFrame({"AI": ai})
    ai_table["AI_category"] = ai_table["AI"].apply(lambda v: idx._categorize(v, idx.AI_CATEGORIES))

    print("Computing EDDI (evaporative demand drought index)...")
    eddi_table = pd.DataFrame({"pet_mm_1mo": pet_monthly})
    for scale in idx.SPI_SCALES:
        eddi_table[f"pet_mm_{scale}mo"] = pet_monthly.rolling(scale, min_periods=scale).sum()
        eddi_table[f"EDDI{scale}"] = idx.compute_eddi(pet_monthly, scale)
        # EDDI's sign convention is the opposite of SPI's: HIGH EDDI (high evaporative
        # demand) is the drought signal, so the same category table is applied to the
        # negated value.
        eddi_table[f"EDDI{scale}_category"] = (-eddi_table[f"EDDI{scale}"]).apply(lambda v: idx._categorize(v, idx.DROUGHT_CATEGORIES))

    print("Computing Water Balance Anomaly...")
    wb_anomaly = idx.compute_water_balance_anomaly(water_balance_monthly)
    wb_table = pd.DataFrame({"water_balance_mm": water_balance_monthly, "water_balance_anomaly_mm": wb_anomaly})
    wb_table["anomaly_category"] = wb_table["water_balance_anomaly_mm"].apply(lambda v: idx._categorize(v / 50.0, idx.DROUGHT_CATEGORIES))

    baseline_note = (f"Data: {BASELINE_START}-{DATA_END} (the final year may be partial -- ERA5-Land's own "
                      f"publication lag means the most recent months aren't out yet). Normal/baseline: full WMO "
                      f"30-year climate normal period {BASELINE_START}-{BASELINE_END} "
                      f"({BASELINE_END - BASELINE_START + 1} years) -- every anomaly/index below is computed "
                      f"against THIS fixed period regardless of how far the data extends. "
                      f"SPEI here uses a per-calendar-month z-score of the FAO-56 Penman-Monteith water balance (P-PET) as a "
                      f"demo-grade stand-in for the published method's 3-parameter log-logistic fit. Decile Index "
                      f"follows IMD's own operational drought classification. CZI and RAI are standardized "
                      f"precipitation indices computed independently of SPI, as cross-checks. EDDI standardizes "
                      f"evaporative demand (PET) the same way SPI standardizes rainfall -- HIGH EDDI is the "
                      f"drought signal here, the opposite sign convention from SPI/SPEI. The IMD departure index "
                      f"matches IMD's own category thresholds (verified against IMD Pune's official glossary) but "
                      f"NOT IMD's baseline years, which weren't available to verify. PDSI/scPDSI/CMI/Palmer "
                      f"Z-Index are NOT included -- they need an assumed soil water-holding capacity and a "
                      f"multi-step accounting procedure this project doesn't have inputs for yet.")

    indices_file = INDICES_DIR / f"chhindwara_drought_indices_{BASELINE_START}_{DATA_END}.xlsx"
    with pd.ExcelWriter(indices_file) as writer:
        spi_table.to_excel(writer, sheet_name="SPI")
        spei_table.to_excel(writer, sheet_name="SPEI")
        pni_table.to_excel(writer, sheet_name="PNI")
        imd_table.to_excel(writer, sheet_name="IMD_Departure")
        weekly_table.to_excel(writer, sheet_name="Weekly")
        fortnightly_table.to_excel(writer, sheet_name="Fortnightly")
        decile_table.to_excel(writer, sheet_name="Decile_IMD")
        czi_table.to_excel(writer, sheet_name="CZI")
        rai_table.to_excel(writer, sheet_name="RAI")
        pci_table.to_excel(writer, sheet_name="PCI")
        ai_table.to_excel(writer, sheet_name="AI")
        eddi_table.to_excel(writer, sheet_name="EDDI")
        wb_table.to_excel(writer, sheet_name="WaterBalanceAnomaly")
        pd.DataFrame({"note": [baseline_note]}).to_excel(writer, sheet_name="README", index=False)
    print(f"  Wrote {indices_file}")

    # Build the temperature anomaly series from the anomalies stage's own output
    # (t2m_mean_anomaly), not by re-subtracting here, so it matches exactly what
    # compute_anomalies_for_year wrote.
    anomaly_files = sorted(ANOMALIES_DIR.glob("t2m/t2m_*_anomaly.nc"))
    anomaly_ds = xr.open_mfdataset([str(f) for f in anomaly_files], combine="by_coords")
    t2m_mean_anomaly_daily = idx._area_mean(anomaly_ds["t2m_mean_anomaly"]).to_series().resample("MS").mean()
    temp_table = pd.DataFrame({"t2m_mean_anomaly": t2m_mean_anomaly_daily})
    temp_table["t2m_category"] = temp_table["t2m_mean_anomaly"].apply(lambda v: idx._categorize(v, idx.TEMP_CATEGORIES))

    print("Drawing SIMPLE figures (manager-facing -- color = severity, no stats background needed)...")
    idx.plot_simple_category_bars(spi_table["SPI3"], spi_table["SPI3_category"], idx._BAND_COLORS,
                               f"Drought status (SPI-3) -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "SPI-3 (0 = normal; more negative = drier)", baseline_note, FIG_DIR / "spi_timeseries.png",
                               subtitle="3-month rainfall total expressed as a standardized deviation from the 1991-2020 calendar-month "
                                        "average. Negative values indicate below-normal rainfall; magnitude indicates severity.",
                               formula=idx.FORMULAS["SPI"])
    idx.plot_simple_category_bars(spei_table["SPEI3"], spei_table["SPEI3_category"], idx._BAND_COLORS,
                               f"Drought status incl. heat stress (SPEI-3) -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "SPEI-3 (0 = normal; more negative = drier)", baseline_note, FIG_DIR / "spei_timeseries.png",
                               subtitle="Standardized deviation of the 3-month water balance (rainfall minus potential evapotranspiration) "
                                        "from its 1991-2020 average. Incorporates evaporative demand in addition to rainfall.",
                               formula=idx.FORMULAS["SPEI"])
    idx.plot_simple_category_bars(pni_table["PNI_pct"], pni_table["PNI_category"], idx._PNI_BAND_COLORS,
                               f"Rainfall as % of normal (PNI) -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "% of normal monthly rain", baseline_note, FIG_DIR / "pni_timeseries.png",
                               subtitle="Monthly rainfall expressed as a percentage of the 1991-2020 average for that calendar month. "
                                        "100% represents the long-term normal.",
                               formula=idx.FORMULAS["PNI"])
    idx.plot_simple_category_bars(imd_table["departure_pct"], imd_table["departure_category"], idx._IMD_BAND_COLORS,
                               f"Rainfall departure from normal, IMD convention -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "% departure from normal (0 = normal)", baseline_note, FIG_DIR / "imd_departure_timeseries.png",
                               subtitle="Monthly rainfall departure from the 1991-2020 average for that calendar month, using India "
                                        "Meteorological Department's own category definitions (Excess/Normal/Deficient/Scanty/No rain). "
                                        "Note: IMD's own published Normal is typically a different (often longer) baseline period than "
                                        "the 1991-2020 WMO period used here -- category labels match IMD's convention, the baseline years do not.",
                               formula=idx.FORMULAS["IMD"], clip_display=(-100, 150))
    idx.plot_simple_category_bars(weekly_table["SPI1"], weekly_table["SPI1_category"], idx._BAND_COLORS,
                               f"Drought status, weekly (SPI, 1-week) -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "SPI (0 = normal; more negative = drier)", baseline_note, FIG_DIR / "spi_weekly_timeseries.png",
                               bar_width=5,
                               subtitle="Each bar is one ISO calendar week's rainfall, standardized against that same week-of-year "
                                        "across all years. Same reading as the monthly SPI chart, at weekly resolution.",
                               formula=idx.FORMULAS["SPI"])
    idx.plot_simple_category_bars(weekly_table["departure_pct"], weekly_table["departure_category"], idx._IMD_BAND_COLORS,
                               f"Rainfall departure, weekly, IMD convention -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "% departure from normal (0 = normal)", baseline_note, FIG_DIR / "imd_departure_weekly_timeseries.png",
                               bar_width=5,
                               subtitle="Weekly rainfall departure from that ISO week's average, using IMD's category definitions.",
                               formula=idx.FORMULAS["IMD"], clip_display=(-100, 150))
    idx.plot_simple_category_bars(fortnightly_table["SPI1"], fortnightly_table["SPI1_category"], idx._BAND_COLORS,
                               f"Drought status, fortnightly (SPI, 1-fortnight) -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "SPI (0 = normal; more negative = drier)", baseline_note, FIG_DIR / "spi_fortnightly_timeseries.png",
                               bar_width=10,
                               subtitle="Each bar is one 14-day period's rainfall, standardized against that same period-of-year "
                                        "across all years. Same reading as the monthly SPI chart, at fortnightly resolution.",
                               formula=idx.FORMULAS["SPI"])
    idx.plot_simple_category_bars(fortnightly_table["departure_pct"], fortnightly_table["departure_category"], idx._IMD_BAND_COLORS,
                               f"Rainfall departure, fortnightly, IMD convention -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "% departure from normal (0 = normal)", baseline_note, FIG_DIR / "imd_departure_fortnightly_timeseries.png",
                               bar_width=10,
                               subtitle="Fortnightly rainfall departure from that 14-day period's average, using IMD's category definitions.",
                               formula=idx.FORMULAS["IMD"], clip_display=(-100, 150))
    # Decile and CZI plots dropped per request (hard to explain in a review
    # meeting) -- both indices are still computed and written to the Excel
    # (CZI correlates ~0.98 with SPI anyway, so little signal is lost by not
    # charting it separately; RAI/PNI/SPI/SPEI remain as the drought views).
    idx.plot_simple_category_bars(rai_table["RAI"], rai_table["RAI_category"], idx._BAND_COLORS,
                               f"Drought status, cross-check (RAI) -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "RAI (0 = normal; more negative = drier)", baseline_note, FIG_DIR / "rai_timeseries.png",
                               subtitle="Monthly rainfall anomaly rescaled by the mean of the ten wettest and ten driest years on record "
                                        "for that calendar month (Van Rooy, 1965).",
                               formula=idx.FORMULAS["RAI"])
    idx.plot_simple_category_bars(temp_table["t2m_mean_anomaly"], temp_table["t2m_category"], idx._TEMP_BAND_COLORS,
                               f"Temperature vs normal -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                               "degC vs normal", baseline_note, FIG_DIR / "temperature_anomaly_timeseries.png",
                               subtitle="Monthly mean temperature expressed as a deviation (degC) from its 1991-2020 calendar-day normal.",
                               formula=idx.FORMULAS["Temp"])
    idx.plot_simple_category_bars(pci_table["PCI"], pci_table["PCI_category"], idx._PCI_BAND_COLORS,
                               f"Rainfall concentration through the year (PCI) -- Chhindwara, {BASELINE_START}-{BASELINE_END}",
                               "PCI (Precipitation Concentration Index)", baseline_note, FIG_DIR / "pci_timeseries.png", bar_width=200,
                               subtitle="Annual distribution of rainfall across the 12 calendar months. Higher values indicate rainfall "
                                        "concentrated in fewer months, raising both flood and drought risk relative to a uniform distribution.",
                               formula=idx.FORMULAS["PCI"])
    idx.plot_simple_category_bars(ai_table["AI"], ai_table["AI_category"], idx._AI_BAND_COLORS,
                               f"Aridity Index (annual P / PET) -- Chhindwara, {BASELINE_START}-{BASELINE_END}",
                               "AI = Annual rainfall / Annual PET", baseline_note, FIG_DIR / "ai_timeseries.png", bar_width=200,
                               subtitle="Ratio of annual rainfall to annual potential evapotranspiration (PET). Values below 0.65 "
                                        "indicate semi-arid conditions (UNEP classification).",
                               formula=idx.FORMULAS["AI"])
    idx.plot_simple_category_bars(eddi_table["EDDI3"], eddi_table["EDDI3_category"], idx._BAND_COLORS,
                               f"Evaporative demand drought status (EDDI-3) -- Chhindwara, {BASELINE_START}-{BASELINE_END}",
                               "EDDI-3 (HIGH = more evaporative stress)", baseline_note, FIG_DIR / "eddi_timeseries.png",
                               subtitle="Standardized evaporative demand (potential evapotranspiration), independent of rainfall. "
                                        "Sign convention is reversed relative to the other indices: positive values indicate drought.",
                               formula=idx.FORMULAS["EDDI"])
    idx.plot_simple_category_bars(wb_table["water_balance_anomaly_mm"], wb_table["anomaly_category"], idx._BAND_COLORS,
                               f"Water balance (Rain-PET) anomaly -- Chhindwara, {BASELINE_START}-{BASELINE_END}",
                               "mm vs normal", baseline_note, FIG_DIR / "water_balance_anomaly_timeseries.png",
                               subtitle="Monthly water balance (rainfall minus potential evapotranspiration) expressed as a deviation "
                                        "from its 1991-2020 average.",
                               formula=idx.FORMULAS["WB"])

    print("Drawing DETAILED (technical) figures -- all 4 accumulation scales...")
    idx.plot_index_small_multiples(spi_table, "SPI", idx.SPI_SCALES,
                                f"SPI (Standardized Precipitation Index), all scales -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                                "SPI (standard deviations from normal)", baseline_note, FIG_DIR / "spi_timeseries_detailed.png",
                                subtitle="SPI computed at four accumulation periods (1/3/6/12 months). Shorter windows resolve "
                                         "short-term anomalies; longer windows resolve sustained, multi-year conditions.",
                                formula=idx.FORMULAS["SPI"])
    idx.plot_index_small_multiples(spei_table, "SPEI", idx.SPI_SCALES,
                                f"SPEI (Standardized Precip-Evapotranspiration Index), all scales -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                                "SPEI (z-score, demo method)", baseline_note, FIG_DIR / "spei_timeseries_detailed.png",
                                subtitle="SPEI computed at four accumulation periods (1/3/6/12 months), incorporating evaporative "
                                         "demand in addition to rainfall.",
                                formula=idx.FORMULAS["SPEI"])
    idx.plot_index_small_multiples(eddi_table, "EDDI", idx.SPI_SCALES,
                                f"EDDI (Evaporative Demand Drought Index), all scales -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                                "EDDI (HIGH = more evaporative stress)", baseline_note, FIG_DIR / "eddi_timeseries_detailed.png",
                                subtitle="EDDI computed at four accumulation periods. Sign convention is reversed relative to "
                                         "SPI/SPEI: positive values indicate increased evaporative demand and drought risk.",
                                formula=idx.FORMULAS["EDDI"])

    print("Computing annual extreme indices (CDD/CWD/R95p/SDII -- pipeline.py's existing ETCCDI stage)...")
    defaults = pl.load_defaults()
    pl.compute_all_indices(["cdd", "cwd", "r95p", "sdii"], BASELINE_START, DATA_END, PROCESSED_DIR,
                            ANNUAL_INDICES_DIR, BASELINE_START, BASELINE_END, defaults=defaults, region_name="chhindwara")

    etccdi_table = pd.DataFrame()
    etccdi_meta = {
        "cdd": ("Longest dry spell (CDD)", "days", "Maximum number of consecutive days with daily rainfall below 1mm "
                "(ETCCDI definition)."),
        "cwd": ("Longest wet spell (CWD)", "days", "Maximum number of consecutive days with daily rainfall of 1mm or "
                "more (ETCCDI definition)."),
        "r95p": ("Heavy-rain contribution (R95p)", "mm", "Total annual rainfall contributed by days exceeding the 95th "
                 "percentile of wet-day rainfall, 1991-2020 (ETCCDI definition)."),
        "sdii": ("Mean wet-day rain intensity (SDII)", "mm/day", "Mean rainfall per wet day (days with rainfall "
                 "≥ 1mm), ETCCDI definition."),
    }
    for name in etccdi_meta:
        files = sorted(ANNUAL_INDICES_DIR.glob(f"{name}_*.nc"))
        if not files:
            continue
        ds = xr.open_mfdataset([str(f) for f in files], combine="by_coords")
        etccdi_table[name] = idx._area_mean(ds[name]).to_series()
    if not etccdi_table.empty:
        etccdi_file = INDICES_DIR / f"chhindwara_annual_extremes_{BASELINE_START}_{DATA_END}.xlsx"
        etccdi_table.to_excel(etccdi_file, sheet_name="Annual_extremes")
        print(f"  Wrote {etccdi_file}")
        etccdi_formula_key = {"cdd": "CDD", "cwd": "CWD", "r95p": "R95p", "sdii": "SDII"}
        for name, (label, unit, explainer) in etccdi_meta.items():
            if name not in etccdi_table.columns:
                continue
            series = etccdi_table[name].dropna()
            idx.plot_simple_category_bars(series, pd.Series("", index=series.index), {"": pl.RAIN_COLOR},
                                       f"{label} -- Chhindwara, {BASELINE_START}-{BASELINE_END} baseline",
                                       unit, baseline_note, FIG_DIR / f"{name}_timeseries.png",
                                       annotate_extremes=True, bar_width=200, subtitle=explainer,
                                       formula=idx.FORMULAS[etccdi_formula_key[name]])

    print("Drawing spatial maps (driest month, wettest month, hottest anomaly month)...")
    spi3 = spi_table["SPI3"].dropna()
    if len(spi3):
        dry_month = spi3.idxmin()
        wet_month = spi3.idxmax()
        for label, month_ts in [("driest", dry_month), ("wettest", wet_month)]:
            y, m = month_ts.year, month_ts.month
            f = ANOMALIES_DIR / "tp" / f"tp_{y}_anomaly.nc"
            if f.exists():
                ds = xr.open_dataset(f)
                pct = ds["tp_sum_pct_of_normal"].sel(time=ds["time"].dt.month == m).mean(dim="time") - 100.0
                idx.plot_spatial_anomaly_map(
                    pct, f"Rainfall anomaly: {label} month on record ({y}-{m:02d}) -- Chhindwara district",
                    "% deviation from the 1991-2020 normal", region, FIG_DIR / f"spatial_precip_anomaly_{label}_{y}_{m:02d}.png",
                    hot_is_red=False, baseline_note=baseline_note,
                    subtitle="Grid-cell rainfall deviation from the 1991-2020 normal for that month. Blue indicates above-normal "
                             "rainfall; red indicates below-normal.")

    hot_month = t2m_mean_anomaly_daily.idxmax() if len(t2m_mean_anomaly_daily.dropna()) else None
    if hot_month is not None:
        y, m = hot_month.year, hot_month.month
        f = ANOMALIES_DIR / "t2m" / f"t2m_{y}_anomaly.nc"
        if f.exists():
            ds = xr.open_dataset(f)
            spatial = ds["t2m_mean_anomaly"].sel(time=ds["time"].dt.month == m).mean(dim="time")
            idx.plot_spatial_anomaly_map(spatial, f"Temperature anomaly: hottest month on record ({y}-{m:02d}) -- Chhindwara district",
                                      "degC deviation from the 1991-2020 normal", region, FIG_DIR / f"spatial_temp_anomaly_{y}_{m:02d}.png",
                                      hot_is_red=True, baseline_note=baseline_note,
                                      subtitle="Grid-cell temperature deviation (degC) from the 1991-2020 normal for that month. "
                                               "Red indicates above-normal temperature; blue indicates below-normal.")

    print("\nDone. Outputs in:", OUT)
    print(" ", baseline_note)


if __name__ == "__main__":
    main()
