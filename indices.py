"""Place-agnostic drought/climate index math and research-grade plotting --
shared by chhindwara_indices.py (the validated Chhindwara run) and
run_climatology.py (the generalized any-place CLI), so there is exactly one
implementation of each formula/chart instead of two copies that can drift.

Extracted from chhindwara_indices.py; nothing here is Chhindwara-specific --
every function takes the place/paths/region it needs as arguments.
"""
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as pl
import era5_client as bt

FIG_DPI = 450  # bumped from 300 for dashboard embedding; same visual style otherwise

# =============================================================================
# Official/government report styling -- an additive alternate palette used
# only when a plotting function is called with official=True. The default
# style (used everywhere else) is completely untouched by this.
# =============================================================================
GOV_WHITE = "#ffffff"
GOV_NAVY = "#0f2a4a"
GOV_NAVY_SECONDARY = "#2c4a6e"
GOV_BAND = "#0f2a4a"
GOV_FOOTER_GRAY = "#6b6b6b"
GOV_BAND_HEIGHT = 0.065


def _official_header(fig, district: str) -> None:
    """Navy institutional header band across the top of the figure -- the one
    visual element that distinguishes the government-report style from the
    default research-caption style. Figure-fraction coordinates, so it scales
    with any figsize."""
    from matplotlib.patches import Rectangle
    fig.patches.append(Rectangle((0, 1 - GOV_BAND_HEIGHT), 1, GOV_BAND_HEIGHT, transform=fig.transFigure,
                                  facecolor=GOV_BAND, edgecolor="none", zorder=10))
    fig.text(0.04, 1 - GOV_BAND_HEIGHT / 2, "GOVERNMENT OF MADHYA PRADESH", transform=fig.transFigure,
              ha="left", va="center", fontsize=9.5, fontweight="bold", color=GOV_WHITE, zorder=11)
    fig.text(0.97, 1 - GOV_BAND_HEIGHT / 2, f"District: {district}", transform=fig.transFigure,
              ha="right", va="center", fontsize=9.5, color=GOV_WHITE, zorder=11)


def _official_footer(fig) -> None:
    fig.text(0.5, 0.01, "Source: ERA5-Land Reanalysis (Copernicus Climate Data Store) -- "
             "methodology aligned with WMO / India Meteorological Department standards",
             transform=fig.transFigure, ha="center", va="bottom", fontsize=7.5, color=GOV_FOOTER_GRAY)


def savefig_retry(fig, out_file: Path, **kwargs) -> None:
    """This project's folder is OneDrive-synced, which intermittently holds a
    transient lock on a file right as it's about to be (re)written -- raising
    OSError [Errno 22] on fig.savefig(). Retrying after a short pause clears it
    every time it's been observed; 5 attempts is comfortably more than needed."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        try:
            fig.savefig(out_file, **kwargs)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(2)


SPI_SCALES = [1, 3, 6, 12]

DROUGHT_CATEGORIES = [
    (2.0, float("inf"), "Extremely wet"),
    (1.5, 2.0, "Severely wet"),
    (1.0, 1.5, "Moderately wet"),
    (-1.0, 1.0, "Near normal"),
    (-1.5, -1.0, "Moderately dry"),
    (-2.0, -1.5, "Severely dry"),
    (float("-inf"), -2.0, "Extremely dry"),
]
PNI_CATEGORIES = [
    (100, float("inf"), "Above normal"),
    (75, 100, "Mild drought"),
    (50, 75, "Moderate drought"),
    (25, 50, "Severe drought"),
    (float("-inf"), 25, "Extreme drought"),
]
TEMP_CATEGORIES = [
    (2.0, float("inf"), "Much hotter than normal"),
    (1.0, 2.0, "Hotter than normal"),
    (-1.0, 1.0, "Normal"),
    (-2.0, -1.0, "Colder than normal"),
    (float("-inf"), -2.0, "Much colder than normal"),
]

PCI_CATEGORIES = [  # Oliver (1980) -- how concentrated the year's rain is into few months vs spread evenly
    (20, float("inf"), "Strongly irregular (concentrated)"),
    (16, 20, "Irregular"),
    (11, 16, "Moderate"),
    (float("-inf"), 11, "Uniform"),
]
AI_CATEGORIES = [  # UNEP aridity classes, AI = annual P / annual PET
    (0.65, float("inf"), "Humid"),
    (0.5, 0.65, "Dry sub-humid"),
    (0.2, 0.5, "Semi-arid"),
    (0.03, 0.2, "Arid"),
    (float("-inf"), 0.03, "Hyper-arid"),
]

# IMD's own operational rainfall-departure categories, verified against IMD
# Pune's official glossary (imdpune.gov.in/Reports/glossary.pdf, p.2,
# "Weekly/Seasonal Rainfall Distribution"). The source states whole-percent
# bounds ("+20% or more", "-19% to +19%", "-20% to -59%", "-60% to -99%",
# "-100%"); continuous computed departures are assigned to these round-number
# cut points (20/-20/-60/-99), which matches the source bands everywhere
# except at the single integer boundary points themselves.
IMD_DEPARTURE_CATEGORIES = [
    (20, float("inf"), "Excess"),
    (-20, 20, "Normal"),
    (-60, -20, "Deficient"),
    (-99, -60, "Scanty"),
    (float("-inf"), -99, "No rain"),
]

_PNI_BAND_COLORS = {"Above normal": "#3987e5", "Mild drought": "#f4c89e", "Moderate drought": "#e07b39",
                     "Severe drought": "#a8431f", "Extreme drought": "#6b1a0a"}
_TEMP_BAND_COLORS = {"Much hotter than normal": "#a8431f", "Hotter than normal": "#e07b39",
                      "Normal": pl.SURFACE, "Colder than normal": "#9ec5f4", "Much colder than normal": "#0d366b"}
_PCI_BAND_COLORS = {"Uniform": "#9ec5f4", "Moderate": pl.SURFACE, "Irregular": "#f4c89e",
                     "Strongly irregular (concentrated)": "#a8431f"}
_AI_BAND_COLORS = {"Humid": "#0d366b", "Dry sub-humid": "#3987e5", "Semi-arid": "#f4c89e",
                    "Arid": "#e07b39", "Hyper-arid": "#a8431f"}
_IMD_BAND_COLORS = {"Excess": "#0d366b", "Normal": pl.SURFACE, "Deficient": "#f4c89e",
                     "Scanty": "#e07b39", "No rain": "#a8431f"}
_BAND_COLORS = {"Extremely wet": "#0d366b", "Severely wet": "#3987e5", "Moderately wet": "#9ec5f4",
                "Near normal": pl.SURFACE, "Moderately dry": "#f4c89e", "Severely dry": "#e07b39",
                "Extremely dry": "#a8431f"}
_DECILE_BAND_COLORS = {"Much above normal": "#0d366b", "Above normal": "#9ec5f4", "Normal": pl.SURFACE,
                        "Below normal": "#f4c89e", "Drought (moderate)": "#e07b39", "Drought (severe)": "#a8431f"}


def _categorize(value: float, bands: list[tuple[float, float, str]]) -> str:
    if pd.isna(value):
        return ""
    for lo, hi, label in bands:
        if lo <= value < hi:
            return label
    return ""


def _area_mean(da: xr.DataArray) -> xr.DataArray:
    return da.mean(dim=[d for d in da.dims if d != "time"], skipna=True)


def load_area_averaged_daily(processed_dir: Path, short: str, var: str) -> pd.Series:
    files = sorted(processed_dir.glob(f"{short}/{short}_*_daily.nc"))
    if not files:
        return pd.Series(dtype=float)
    ds = xr.open_mfdataset([str(f) for f in files], combine="by_coords")
    series = _area_mean(ds[var]).to_series()
    series.index.name = "date"
    return series


# =============================================================================
# Weekly / fortnightly accumulation -- same SPI/PNI/IMD-departure math as the
# monthly versions, just grouped by ISO week number or fortnight-of-year
# instead of calendar month.
# =============================================================================

def resample_weekly(daily: pd.Series) -> pd.Series:
    """ISO-week sums (Mon-Sun), indexed by that week's Monday -- not pandas'
    generic 'W' resampler, which doesn't follow ISO week numbering at year
    boundaries (the last days of December can belong to ISO week 1 of the
    next year, and vice versa for early January)."""
    daily = daily.dropna()
    iso = daily.index.isocalendar()
    key = pd.MultiIndex.from_arrays([iso["year"].values, iso["week"].values])
    totals = daily.groupby(key).sum()
    starts = pd.Series(daily.index.values, index=key).groupby(level=[0, 1]).min()
    out = pd.Series(totals.values, index=pd.to_datetime(starts.values)).sort_index()
    out.index.name = "week_start"
    return out


def resample_fortnightly(daily: pd.Series) -> pd.Series:
    """14-day sums within each calendar year (periods 1-26, plus a trailing
    partial period for day 365/366), indexed by that period's first date."""
    daily = daily.dropna()
    fortnight_of_year = (daily.index.dayofyear - 1) // 14
    key = pd.MultiIndex.from_arrays([daily.index.year, fortnight_of_year])
    totals = daily.groupby(key).sum()
    starts = pd.Series(daily.index.values, index=key).groupby(level=[0, 1]).min()
    out = pd.Series(totals.values, index=pd.to_datetime(starts.values)).sort_index()
    out.index.name = "fortnight_start"
    return out


def iso_week_key(series: pd.Series) -> pd.Series:
    return pd.Series(series.index.isocalendar().week.values, index=series.index)


def fortnight_key(series: pd.Series) -> pd.Series:
    return pd.Series((series.index.dayofyear - 1) // 14, index=series.index)


# =============================================================================
# SPI
# =============================================================================

def compute_spi_generic(accum: pd.Series, period_key: pd.Series) -> pd.Series:
    """Gamma-fit standardization grouped by an arbitrary recurring period --
    calendar month for standard (monthly) SPI, ISO week number or
    fortnight-of-year for the short-window variants. `period_key` must be a
    Series aligned to `accum`'s index giving each row's group id."""
    spi = pd.Series(index=accum.index, dtype=float)
    for g in period_key.unique():
        vals = accum[(period_key == g).values].dropna()
        if len(vals) < 5:
            continue
        zero_mask = vals <= 0.0
        q = zero_mask.mean()
        nonzero = vals[~zero_mask]
        h = pd.Series(index=vals.index, dtype=float)
        h[zero_mask] = q
        if len(nonzero) >= 3:
            shape, _, scale_param = stats.gamma.fit(nonzero, floc=0)
            cdf = stats.gamma.cdf(nonzero, shape, loc=0, scale=scale_param)
            h.loc[nonzero.index] = q + (1 - q) * cdf
        else:
            h.loc[nonzero.index] = q + (1 - q) * 0.5
        h = h.clip(1e-6, 1 - 1e-6)
        spi.loc[vals.index] = stats.norm.ppf(h)
    return spi


def compute_spi(monthly_precip: pd.Series, scale: int) -> pd.Series:
    accum = monthly_precip.rolling(scale, min_periods=scale).sum()
    return compute_spi_generic(accum, pd.Series(accum.index.month, index=accum.index))


# =============================================================================
# PET -- FAO-56 Penman-Monteith (preferred) with Hargreaves (temperature-only)
# as a documented fallback when the extra inputs aren't available.
# =============================================================================

def _latest_common_month(shorts: list[str], raw_dir: Path) -> tuple[int, int]:
    """Latest (year, month) present on disk for every one of `shorts` -- daily_fields()
    assumes every requested month-file exists and errors otherwise, so the request range
    must stop at whatever ERA5-Land has actually published so far, not at the nominal end year."""
    common = None
    for short in shorts:
        months = {(int(f.stem.split("_")[1]), int(f.stem.split("_")[2])) for f in (raw_dir / short).glob(f"{short}_*.nc")}
        common = months if common is None else (common & months)
    if not common:
        raise RuntimeError(f"No raw month-files found common to {shorts} in {raw_dir}")
    return max(common)


def penman_monteith_pet_daily(region: pl.Region, raw_dir: Path, start_year: int, end_year: int) -> pd.Series:
    """FAO-56 Penman-Monteith reference ET0 (mm/day), full energy-balance method --
    reuses backtest.py's existing fao56_et0() (ported from the senior team's
    Pandhurna citrus system) instead of the temperature-only Hargreaves
    approximation below. Needs t2m, tp, d2m, u10, v10, ssrd, sp all covering
    the requested years; clips to the same polygon tp/t2m use so the water
    balance stays internally consistent."""
    shorts = ["t2m", "tp", "d2m", "u10", "v10", "ssrd", "sp"]
    last_y, last_m = _latest_common_month(shorts, raw_dir)
    end_y, end_m = min((end_year, 12), (last_y, last_m))
    months = bt.months_between(date(start_year, 1, 1), date(end_y, end_m, 1))
    ds = bt.daily_fields(["t2m", "tp", "d2m", "u10", "ssrd", "sp"], months, raw_dir)
    ds = pl.clip_to_polygon(ds, region)
    pet = _area_mean(ds["et0_mm"]).to_series()
    pet.index.name = "date"
    return pet


def hargreaves_pet_daily(tmax: pd.Series, tmin: pd.Series, tmean: pd.Series, lat_deg: float) -> pd.Series:
    """FAO-56 Hargreaves (1985) reference ET0, mm/day, using only daily temperature.
    Fallback when d2m/u10/v10/ssrd/sp aren't available for Penman-Monteith."""
    doy = tmax.index.dayofyear.to_numpy()
    phi = np.radians(lat_deg)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365)
    delta = 0.409 * np.sin(2 * np.pi * doy / 365 - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(delta), -1, 1))
    ra_mj = (24 * 60 / np.pi) * 0.0820 * dr * (ws * np.sin(phi) * np.sin(delta) + np.cos(phi) * np.cos(delta) * np.sin(ws))
    ra_mm = 0.408 * ra_mj

    tr = (tmax - tmin).clip(lower=0)
    et0 = 0.0023 * (tmean + 17.8) * np.sqrt(tr) * ra_mm
    return pd.Series(et0.to_numpy(), index=tmax.index, name="pet_mm")


def compute_spei_zscore(water_balance_monthly: pd.Series, scale: int) -> pd.Series:
    """Demo-grade SPEI: rolling-sum water balance, standardized as a z-score per
    calendar month across the baseline years. The published SPEI method fits a
    3-parameter log-logistic distribution instead of assuming normality; this
    is a simpler stand-in for a first demo, noted as such wherever it's used."""
    accum = water_balance_monthly.rolling(scale, min_periods=scale).sum()
    spei = pd.Series(index=accum.index, dtype=float)
    for m in range(1, 13):
        vals = accum[accum.index.month == m].dropna()
        if len(vals) < 5:
            continue
        mu, sigma = vals.mean(), vals.std()
        if sigma == 0:
            continue
        spei.loc[vals.index] = (vals - mu) / sigma
    return spei


# =============================================================================
# PNI (Percent of Normal precipitation Index)
# =============================================================================

def compute_pni(precip: pd.Series, period_key: pd.Series | None = None) -> pd.Series:
    """period_key defaults to calendar month (standard monthly PNI); pass ISO
    week number or fortnight-of-year for the short-window variants."""
    key = period_key if period_key is not None else pd.Series(precip.index.month, index=precip.index)
    normal_by_period = precip.groupby(key.values).mean()
    return precip / key.map(normal_by_period) * 100.0


# =============================================================================
# IMD-style percentage departure -- matches India Meteorological Department's
# own operational convention, verified against IMD Pune's official glossary
# (https://www.imdpune.gov.in/Reports/glossary.pdf, p.2, "Weekly/Seasonal
# Rainfall Distribution"). Deliberately a SEPARATE index from PNI above, not a
# relabeling of it: PNI is ratio-form (100 = normal), IMD's real convention is
# departure-form (0 = normal) with different category cutoffs and labels.
# =============================================================================

def compute_imd_departure(precip: pd.Series, period_key: pd.Series | None = None) -> pd.Series:
    """(actual - normal) / normal * 100, normal = this period's mean over the
    series -- same normal PNI uses, just expressed as a departure from zero
    instead of a ratio to 100. period_key defaults to calendar month; pass
    ISO week number or fortnight-of-year for the short-window variants."""
    key = period_key if period_key is not None else pd.Series(precip.index.month, index=precip.index)
    normal_by_period = precip.groupby(key.values).mean()
    normal = key.map(normal_by_period)
    return (precip - normal) / normal * 100.0


# =============================================================================
# Decile-based Drought Index -- IMD's operational drought classification.
# =============================================================================

DECILE_CATEGORIES = [
    (9, 11, "Much above normal"),
    (7, 9, "Above normal"),
    (5, 7, "Normal"),
    (3, 5, "Below normal"),
    (2, 3, "Drought (moderate)"),
    (0, 2, "Drought (severe)"),
]


def _decile_category(decile: float) -> str:
    if pd.isna(decile):
        return ""
    for lo, hi, label in DECILE_CATEGORIES:
        if lo <= decile < hi:
            return label
    return ""


def compute_decile_index(monthly_precip: pd.Series) -> pd.Series:
    decile = pd.Series(index=monthly_precip.index, dtype=float)
    for m in range(1, 13):
        vals = monthly_precip[monthly_precip.index.month == m]
        ranks = vals.rank(pct=True)  # 0-1, fraction of years this value exceeds
        decile.loc[vals.index] = ranks * 10.0
    return decile


# =============================================================================
# CZI (modified China-Z Index) -- skewness-aware standardized index, as a
# cross-check against SPI (Wu et al. 2001).
# =============================================================================

def compute_czi(monthly_precip: pd.Series, scale: int) -> pd.Series:
    accum = monthly_precip.rolling(scale, min_periods=scale).sum()
    czi = pd.Series(index=accum.index, dtype=float)
    for m in range(1, 13):
        vals = accum[accum.index.month == m].dropna()
        if len(vals) < 5:
            continue
        mean, std = vals.mean(), vals.std()
        cs = stats.skew(vals)  # coefficient of skewness
        if cs == 0 or std == 0:
            continue
        z = (vals - mean) / std
        term = (cs / 2) * z + 1
        term = term.clip(lower=1e-6)  # guard against a negative base with a fractional exponent
        czi.loc[vals.index] = (6 / cs) * (term ** (1 / 3)) - (6 / cs) + (cs / 6)
    return czi


# =============================================================================
# RAI (Rainfall Anomaly Index, Van Rooy 1965).
# =============================================================================

def compute_rai(monthly_precip: pd.Series) -> pd.Series:
    rai = pd.Series(index=monthly_precip.index, dtype=float)
    for m in range(1, 13):
        vals = monthly_precip[monthly_precip.index.month == m]
        if len(vals) < 10:
            continue
        mean = vals.mean()
        n = max(1, min(len(vals) // 10, len(vals) // 2))  # top/bottom decile count, Van Rooy's original definition
        sorted_vals = vals.sort_values()
        bottom_mean = sorted_vals.iloc[:n].mean()
        top_mean = sorted_vals.iloc[-n:].mean()
        for idx, val in vals.items():
            if val >= mean:
                denom = (top_mean - mean)
                rai.loc[idx] = 3 * (val - mean) / denom if denom else np.nan
            else:
                denom = (mean - bottom_mean)
                rai.loc[idx] = -3 * (mean - val) / denom if denom else np.nan
    return rai


# =============================================================================
# PCI (Precipitation Concentration Index, Oliver 1980).
# =============================================================================

def compute_pci(monthly_precip: pd.Series) -> pd.Series:
    pci = pd.Series(dtype=float)
    for year, g in monthly_precip.groupby(monthly_precip.index.year):
        if len(g) < 12:
            continue
        pci.loc[pd.Timestamp(year=year, month=1, day=1)] = 100 * (g ** 2).sum() / (g.sum() ** 2)
    return pci.sort_index()


# =============================================================================
# AI (Aridity Index, UNEP) -- annual precip / annual PET.
# =============================================================================

def compute_aridity_index(monthly_precip: pd.Series, monthly_pet: pd.Series) -> pd.Series:
    annual_p = monthly_precip.resample("YS").sum()
    annual_pet = monthly_pet.resample("YS").sum()
    return annual_p / annual_pet.where(annual_pet > 0)


# =============================================================================
# EDDI (Evaporative Demand Drought Index) -- SPI's standardization applied to
# PET instead of rainfall. HIGH EDDI = drought risk (opposite sign of SPI).
# =============================================================================

def compute_eddi(monthly_pet: pd.Series, scale: int) -> pd.Series:
    return compute_spi(monthly_pet, scale)  # identical gamma-fit procedure, different input series


# =============================================================================
# Water Balance Anomaly -- plain (P-PET) departure from its own climatological
# monthly normal, in mm.
# =============================================================================

def compute_water_balance_anomaly(water_balance_monthly: pd.Series) -> pd.Series:
    normal_by_month = water_balance_monthly.groupby(water_balance_monthly.index.month).mean()
    return water_balance_monthly - water_balance_monthly.index.month.map(normal_by_month)


# =============================================================================
# Figures
# =============================================================================
# Two styles per index:
#   - "_simple" : a single colored-bar chart, color = drought category directly
#     (traffic-light style) -- readable without knowing what a z-score is.
#   - everything else: the fuller, multi-scale technical version.

# Exact formula for each index, rendered directly on its figure (mathtext) --
# ties every chart to its actual computation instead of just a plain-English
# gloss, the way a published climatology figure would.
FORMULAS = {
    "SPI": r"SPI = $\Phi^{-1}[F(x)]$" "\n" "F = gamma CDF of rainfall, fit per calendar month",
    "SPEI": r"SPEI = $z(P - PET)$" "\n" "z-score of water balance, fit per calendar month",
    "PNI": r"$PNI = \dfrac{P}{\overline{P}_{month}} \times 100$",
    "IMD": r"$Departure\% = \dfrac{P-\overline{P}_{month}}{\overline{P}_{month}} \times 100$" "\n"
           "IMD Pune glossary bands: Excess/Normal/Deficient/Scanty/No rain",
    "Decile": r"$Decile = rank_{10}(P_{month})$" "\n" "ranked within the baseline history of that calendar month",
    "CZI": "Wilson-Hilferty cube-root transform of standardized $P$\n(skewness-corrected alternative to SPI)",
    "RAI": r"$RAI = \pm 3\dfrac{|P-\overline{P}|}{|\overline{P}_{extreme10}-\overline{P}|}$ (Van Rooy 1965)",
    "PCI": r"$PCI = 100\dfrac{\sum_{i=1}^{12}P_i^2}{(\sum_{i=1}^{12}P_i)^2}$ (Oliver 1980)",
    "AI": r"$AI = \dfrac{P_{annual}}{PET_{annual}}$ (UNEP)",
    "EDDI": r"$EDDI = \Phi^{-1}[F(PET)]$" "\n" "Same method as SPI, applied to PET instead of rainfall",
    "WB": r"$WB = P - PET$ (FAO-56 Penman-Monteith)" "\n" r"Anomaly $= WB - \overline{WB}_{month}$",
    "Temp": r"Anomaly $= T - \overline{T}_{doy}$" "\n" "5-day circular-smoothed day-of-year normal",
    "CDD": r"Longest run of days with $P < 1mm$ (ETCCDI)",
    "CWD": r"Longest run of days with $P \geq 1mm$ (ETCCDI)",
    "R95p": r"$\sum P_i$ for days with $P_i > P_{95}$" "\n" "$P_{95}$ = wet-day 95th percentile (ETCCDI)",
    "SDII": r"$SDII = \dfrac{\sum P_i}{n(P_i \geq 1mm)}$ (ETCCDI)",
}


def plot_simple_category_bars(series: pd.Series, category_series: pd.Series, band_colors: dict,
                               title: str, ylabel: str, baseline_note: str, out_file: Path,
                               annotate_extremes: bool = True, bar_width: int = 20,
                               subtitle: str = "", formula: str = "",
                               clip_display: tuple[float, float] | None = None,
                               official: bool = False, district: str = "Chhindwara") -> None:
    import textwrap
    ink_primary = GOV_NAVY if official else pl.INK_PRIMARY
    ink_secondary = GOV_NAVY_SECONDARY if official else pl.INK_SECONDARY
    surface = GOV_WHITE if official else pl.SURFACE
    series = series.dropna()
    category_series = category_series.reindex(series.index)

    # Bar HEIGHT only, when set -- a handful of outlier months (e.g. a
    # naturally dry calendar month where any rain at all is a huge percent
    # of a tiny normal) can otherwise compress every other bar into an
    # unreadable sliver near zero. Category color, worst/best-month
    # detection, and the underlying Excel data all stay on the real value --
    # only the plotted bar top is capped.
    display_series = series.clip(*clip_display) if clip_display else series

    # Auto-detect and state the bar resolution up front -- "below zero = drier"
    # reads very differently depending on whether a bar is one month or one
    # year, and that was previously only inferable from the x-axis tick spacing.
    median_gap_days = pd.Series(series.index).diff().dt.days.median()
    is_annual = pd.notna(median_gap_days) and median_gap_days > 300
    resolution_tag = "One bar = one year. " if is_annual else "One bar = one month. "
    if clip_display:
        resolution_tag += f"Bars capped at {clip_display[0]:g}/{clip_display[1]:g} for readability -- exact values in the Excel output. "
    full_subtitle = resolution_tag + subtitle if subtitle else resolution_tag.strip()

    wrapped_subtitle = "\n".join(textwrap.wrap(full_subtitle, width=138))
    n_sub_lines = wrapped_subtitle.count("\n") + 1 if wrapped_subtitle else 0

    formula_lines = formula.count("\n") + 1 if formula else 0
    band_pad = GOV_BAND_HEIGHT + 0.015 if official else 0
    fig, ax = plt.subplots(figsize=(13, 6.6 + 0.22 * max(n_sub_lines - 1, 0) + 0.3 * (formula_lines > 0)),
                            facecolor=surface)
    top_margin = 0.80 - 0.04 * n_sub_lines - (0.045 if formula_lines else 0) - band_pad
    fig.subplots_adjust(top=top_margin, bottom=0.1 if official else 0.08, left=0.07, right=0.97)
    pl._style_axes(ax)
    ax.set_facecolor(surface)
    colors = [band_colors.get(c, pl.INK_MUTED) for c in category_series]
    ax.bar(display_series.index, display_series.values, color=colors, width=bar_width, linewidth=0)
    ax.axhline(0, color=pl.INK_MUTED, linewidth=0.9)

    # Pad the y-range generously, then always anchor callouts near the TOP of
    # that range (never below the bars) so they can never collide with the
    # x-axis tick labels or the footnote underneath the plot.
    ymin, ymax = min(0, display_series.min()), max(0, display_series.max())
    span = (ymax - ymin) or 1.0

    if annotate_extremes and len(series) > 1:
        worst_date, best_date = series.idxmin(), series.idxmax()
        # Data spaced ~1yr apart (PCI/AI/ETCCDI) gets just the year in callouts
        # ("1992"), not "Jan 1992" -- there's no real month to speak of there.
        date_fmt = "%Y" if is_annual else "%b %Y"
        # Order the two callouts chronologically (not by worst/best) so each
        # label's text extends AWAY from the other point -- the arrow and
        # anchor may be close in time, but the text blocks never are.
        pts = sorted([(worst_date, series.loc[worst_date]), (best_date, series.loc[best_date])],
                     key=lambda p: p[0])
        total_days = max((series.index.max() - series.index.min()).days, 1)
        gap_days = abs((pts[1][0] - pts[0][0]).days)
        close = gap_days < 0.08 * total_days
        extra_pad = 0.44 if close else 0.32
        ax.set_ylim(ymin - 0.12 * span, ymax + extra_pad * span)
        callout_y = ymax + 0.26 * span
        x_nudge = pd.Timedelta(days=max(0.015 * total_days, 1))
        xmin_data, xmax_data = series.index.min(), series.index.max()
        for i, (d, val) in enumerate(pts):
            cat = category_series.loc[d]
            label = f"{d.strftime(date_fmt)}: {cat}" if cat else d.strftime(date_fmt)
            if i == 0:
                ha, text_x, text_y = "right", d - x_nudge, callout_y
            else:
                ha, text_x, text_y = "left", d + x_nudge, callout_y + (0.10 * span if close else 0)
            # Override when the point sits near the plot's own left/right edge --
            # extending the text further toward that edge would run it into the
            # y-axis tick labels or off the canvas, regardless of the other point.
            frac = (d - xmin_data).days / total_days
            if frac < 0.1:
                ha, text_x = "left", d + x_nudge
            elif frac > 0.9:
                ha, text_x = "right", d - x_nudge
            ax.annotate(label, xy=(d, display_series.loc[d]), xytext=(text_x, text_y), textcoords="data",
                        ha=ha, va="bottom", fontsize=8.5, color=ink_primary, fontweight="bold",
                        arrowprops=dict(arrowstyle="-", color=pl.INK_MUTED, linewidth=0.8))
    else:
        ax.set_ylim(ymin - 0.12 * span, ymax + 0.32 * span)

    ax.set_ylabel(ylabel, color=ink_secondary, fontsize=10)
    title_y = 0.965 - band_pad
    fig.suptitle(title, fontsize=14, fontweight="bold", x=0.07, ha="left", y=title_y, color=ink_primary)
    fig.text(0.07, title_y - 0.045, wrapped_subtitle, fontsize=9.5, color=ink_secondary, ha="left", va="top")
    handles = [Patch(facecolor=c, label=label) for label, c in band_colors.items()
               if label not in ("Near normal", "Normal", "")]
    if handles:
        legend_y = top_margin + 0.065 + (0.045 if formula_lines else 0)
        fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, legend_y),
                   ncol=min(len(handles), 6), fontsize=8.5, frameon=False)
    if formula:
        # Formula gets its own dedicated row in the reserved header strip,
        # just above the axes -- NOT inside the axes, since a corner placed
        # there collides with bars on any chart whose values never go
        # negative (PCI, AI, the ETCCDI extremes all have no empty corner).
        fig.text(0.97, top_margin + 0.015, formula, fontsize=8.5, family="monospace", color=ink_secondary,
                  ha="right", va="bottom",
                  bbox=dict(facecolor=surface, edgecolor=pl.BASELINE, boxstyle="round,pad=0.4", alpha=0.92))
    if official:
        _official_header(fig, district)
        _official_footer(fig)
    savefig_retry(fig, out_file, dpi=FIG_DPI, facecolor=surface)
    plt.close(fig)


def _shade_drought_bands(ax, categories=DROUGHT_CATEGORIES) -> None:
    band_colors = {"Extremely wet": "#0d366b", "Severely wet": "#3987e5", "Moderately wet": "#9ec5f4",
                   "Near normal": pl.SURFACE, "Moderately dry": "#f4c89e", "Severely dry": "#e07b39",
                   "Extremely dry": "#a8431f"}
    for lo, hi, label in categories:
        lo_b, hi_b = max(lo, -3.5), min(hi, 3.5)
        ax.axhspan(lo_b, hi_b, color=band_colors.get(label, pl.SURFACE), alpha=0.25, zorder=0)


def plot_index_small_multiples(table: pd.DataFrame, index_prefix: str, scales: list[int], title: str,
                                ylabel: str, baseline_note: str, out_file: Path, subtitle: str = "",
                                formula: str = "", official: bool = False, district: str = "Chhindwara") -> None:
    """Research-grade figure: one panel per accumulation scale (1/3/6/12-month),
    each with its own drought-category shading, plus a shared category legend
    and a methods footnote -- so the figure is self-explanatory on its own,
    not dependent on surrounding text."""
    import textwrap
    ink_primary = GOV_NAVY if official else pl.INK_PRIMARY
    ink_secondary = GOV_NAVY_SECONDARY if official else pl.INK_SECONDARY
    surface = GOV_WHITE if official else pl.SURFACE
    full_subtitle = "One bar-width = one month of the underlying series. " + subtitle if subtitle else ""
    wrapped_subtitle = "\n".join(textwrap.wrap(full_subtitle, width=128)) if full_subtitle else ""
    n_sub_lines = wrapped_subtitle.count("\n") + 1 if wrapped_subtitle else 0
    formula_lines = formula.count("\n") + 1 if formula else 0
    band_pad = GOV_BAND_HEIGHT + 0.015 if official else 0
    extra_h = (0.22 * n_sub_lines if subtitle else 0) + (0.35 * formula_lines if formula else 0) + (1.2 * band_pad if official else 0)
    fig, axes = plt.subplots(len(scales), 1, figsize=(12, 2.4 * len(scales) + 1.2 + extra_h),
                              facecolor=surface, sharex=True)
    if len(scales) == 1:
        axes = [axes]

    scale_labels = {1: "1 month", 3: "3 months", 6: "6 months", 12: "12 months"}
    for ax, scale in zip(axes, scales):
        col = f"{index_prefix}{scale}"
        pl._style_axes(ax)
        ax.set_facecolor(surface)
        _shade_drought_bands(ax)
        series = table[col].dropna()
        ax.plot(series.index, series.values, color=ink_primary, linewidth=1.3)
        ax.fill_between(series.index, 0, series.values,
                         where=series.values >= 0, color=pl.RAIN_COLOR, alpha=0.35, linewidth=0)
        ax.fill_between(series.index, 0, series.values,
                         where=series.values < 0, color=pl.TEMP_COLOR, alpha=0.35, linewidth=0)
        ax.axhline(0, color=pl.INK_MUTED, linewidth=0.8)
        ax.set_ylim(-3.5, 3.5)
        scale_label = scale_labels.get(scale, f"{scale}mo")
        ax.set_ylabel(f"{index_prefix}-{scale}\n({scale_label})", color=ink_secondary, fontsize=9.5, fontweight="bold")

    # Figure-level title/subtitle/legend, stacked top-to-bottom in that fixed
    # order and independent of axes[0]'s own title -- putting the title on
    # axes[0] directly (as before) let the figure-level subtitle/legend above
    # it float ABOVE the title instead of below.
    fig.supylabel(ylabel, color=ink_secondary, fontsize=10)
    title_y = 0.975 - band_pad
    fig.suptitle(title, fontsize=14, fontweight="bold", x=0.08, ha="left", y=title_y, color=ink_primary)
    cursor_y = title_y - 0.028
    if wrapped_subtitle:
        fig.text(0.08, cursor_y, wrapped_subtitle, fontsize=9.5, color=ink_secondary, ha="left", va="top")
        cursor_y -= 0.024 * n_sub_lines + 0.012

    if formula:
        cursor_y -= 0.012
        fig.text(0.97, cursor_y, formula, fontsize=8, family="monospace", color=ink_secondary,
                  ha="right", va="top",
                  bbox=dict(facecolor=surface, edgecolor=pl.BASELINE, boxstyle="round,pad=0.4", alpha=0.92))
        cursor_y -= 0.032 * (formula.count("\n") + 1) + 0.015

    handles = [Patch(facecolor=c, label=label, alpha=0.6) for label, c in _BAND_COLORS.items() if label != "Near normal"]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, cursor_y), ncol=6, fontsize=8, frameon=False)
    top_rect = cursor_y - 0.045
    fig.tight_layout(rect=(0, 0.02 if official else 0.01, 1, top_rect))

    if official:
        _official_header(fig, district)
        _official_footer(fig)
    savefig_retry(fig, out_file, dpi=FIG_DPI, facecolor=surface)
    plt.close(fig)


def plot_index_timeseries(table: pd.DataFrame, cols: list[str], title: str, ylabel: str, out_file: Path,
                           shade_drought: bool = True, baseline_note: str = "") -> None:
    fig, ax = plt.subplots(figsize=(12, 4.8), facecolor=pl.SURFACE)
    pl._style_axes(ax)
    if shade_drought:
        _shade_drought_bands(ax)
    colors = [pl.TEMP_COLOR, pl.RAIN_COLOR, pl.INK_SECONDARY, "#6da7ec"]
    for i, col in enumerate(cols):
        ax.plot(table.index, table[col], color=colors[i % len(colors)], linewidth=1.6, label=col)
    ax.axhline(0, color=pl.INK_MUTED, linewidth=0.8)
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left", color=pl.INK_PRIMARY)
    ax.set_ylabel(ylabel, color=pl.INK_SECONDARY, fontsize=10)
    ax.legend(loc="upper right", fontsize=9, frameon=False)
    fig.tight_layout(rect=(0, 0.01, 1, 1))
    savefig_retry(fig, out_file, dpi=FIG_DPI, facecolor=pl.SURFACE)
    plt.close(fig)


def plot_decile_timeseries(table: pd.DataFrame, title: str, baseline_note: str, out_file: Path,
                            subtitle: str = "", formula: str = "") -> None:
    """IMD's own operational drought classification -- decile 1-10 scale,
    plotted on its own since its bands/scale differ from SPI/SPEI's z-scores."""
    import textwrap
    full_subtitle = "One point = one month. " + subtitle if subtitle else ""
    wrapped_subtitle = "\n".join(textwrap.wrap(full_subtitle, width=128)) if full_subtitle else ""
    n_sub_lines = wrapped_subtitle.count("\n") + 1 if wrapped_subtitle else 0

    formula_lines = formula.count("\n") + 1 if formula else 0
    fig, ax = plt.subplots(figsize=(12, 6.1 + 0.22 * max(n_sub_lines - 1, 0) + 0.3 * (formula_lines > 0)),
                            facecolor=pl.SURFACE)
    top_margin = 0.80 - (0.04 * n_sub_lines if subtitle else 0) - (0.045 if formula_lines else 0)
    fig.subplots_adjust(top=top_margin, bottom=0.08, left=0.08, right=0.97)
    pl._style_axes(ax)
    for lo, hi, label in DECILE_CATEGORIES:
        ax.axhspan(lo, hi, color=_DECILE_BAND_COLORS.get(label, pl.SURFACE), alpha=0.3, zorder=0)
    series = table["decile"].dropna()
    ax.plot(series.index, series.values, color=pl.INK_PRIMARY, linewidth=1.4)
    ax.axhline(5, color=pl.INK_MUTED, linewidth=0.8, linestyle="--")
    ax.set_ylim(0, 10)
    ax.set_ylabel("Decile (1 = driest 10%, 10 = wettest 10%)", color=pl.INK_SECONDARY, fontsize=9)
    title_y = 0.965
    fig.suptitle(title, fontsize=13, fontweight="bold", x=0.08, ha="left", y=title_y, color=pl.INK_PRIMARY)
    if wrapped_subtitle:
        fig.text(0.08, title_y - 0.05, wrapped_subtitle, fontsize=9.5, color=pl.INK_SECONDARY, ha="left", va="top")
    handles = [Patch(facecolor=c, label=label, alpha=0.5) for label, c in _DECILE_BAND_COLORS.items() if label != "Normal"]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, top_margin + 0.07 + (0.045 if formula_lines else 0)),
               ncol=5, fontsize=8, frameon=False)
    if formula:
        # Own header row, not inside the axes -- decile ranges 0-10 (always
        # positive), so a bottom-corner box would collide with the line.
        fig.text(0.97, top_margin + 0.015, formula, fontsize=8, family="monospace", color=pl.INK_SECONDARY,
                  ha="right", va="bottom",
                  bbox=dict(facecolor=pl.SURFACE, edgecolor=pl.BASELINE, boxstyle="round,pad=0.4", alpha=0.92))
    savefig_retry(fig, out_file, dpi=FIG_DPI, facecolor=pl.SURFACE)
    plt.close(fig)


def plot_spatial_anomaly_map(da: xr.DataArray, title: str, cbar_label: str, region: pl.Region, out_file: Path,
                              hot_is_red: bool = False, baseline_note: str = "", subtitle: str = "",
                              official: bool = False, district: str = "Chhindwara") -> None:
    """hot_is_red=True for temperature (conventional: red=hot, blue=cold);
    False for precipitation (conventional: red=dry/below normal, blue=wet/above normal,
    i.e. negative values red, positive blue -- the opposite sense of temperature)."""
    import textwrap
    from matplotlib.colors import TwoSlopeNorm

    ink_primary = GOV_NAVY if official else pl.INK_PRIMARY
    ink_secondary = GOV_NAVY_SECONDARY if official else pl.INK_SECONDARY
    surface = GOV_WHITE if official else pl.SURFACE
    band_pad = GOV_BAND_HEIGHT + 0.015 if official else 0
    x_dim = "longitude" if "longitude" in da.dims else "lon"
    y_dim = "latitude" if "latitude" in da.dims else "lat"
    vmax = float(np.nanmax(np.abs(da.values))) or 1.0
    norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
    cmap = "RdBu_r" if hot_is_red else "RdBu"

    # Wide enough that a long title never overflows the right edge (the earlier
    # 7in-wide figure clipped titles like "...2006-02) -- Chhindwara").
    fig, ax = plt.subplots(figsize=(9.5, 7.2), facecolor=surface)
    ax.set_facecolor(surface)
    mesh = ax.pcolormesh(da[x_dim], da[y_dim], da.values, cmap=cmap, norm=norm, shading="auto")
    ax.set_aspect("equal")
    ax.grid(True, color=pl.GRIDLINE, linewidth=0.5, alpha=0.6, zorder=0)
    cbar = fig.colorbar(mesh, ax=ax, shrink=0.85, pad=0.03)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(colors=pl.INK_MUTED, labelsize=9)
    cbar.set_label(cbar_label, color=ink_secondary, fontsize=9)

    if region.polygon is not None:
        xs, ys = region.polygon.exterior.xy
        ax.plot(xs, ys, color=ink_primary, linewidth=1.8)

    mean_val = float(np.nanmean(da.values))
    stats_box = f"District average: {mean_val:+.1f}\nRange: {float(np.nanmin(da.values)):+.1f} to {float(np.nanmax(da.values)):+.1f}"
    ax.text(0.02, 0.02, stats_box, transform=ax.transAxes, ha="left", va="bottom", fontsize=9,
             color=ink_secondary, bbox=dict(facecolor=surface, edgecolor=pl.BASELINE, boxstyle="round,pad=0.4"))

    wrapped_title = "\n".join(textwrap.wrap(title, width=48))
    title_y = 0.98 - band_pad
    fig.suptitle(wrapped_title, fontsize=13, fontweight="bold", x=0.06, ha="left", y=title_y, color=ink_primary)
    subtitle_y = title_y - 0.045 * (wrapped_title.count("\n") + 1) - 0.015
    if subtitle:
        wrapped_sub = "\n".join(textwrap.wrap(subtitle, width=75))
        fig.text(0.06, subtitle_y, wrapped_sub, fontsize=9.5, color=ink_secondary, ha="left", va="top")
        top_rect = subtitle_y - 0.03 * (wrapped_sub.count("\n") + 1)
    else:
        top_rect = subtitle_y
    ax.set_xlabel("longitude", color=ink_secondary, fontsize=9)
    ax.set_ylabel("latitude", color=ink_secondary, fontsize=9)
    ax.tick_params(colors=pl.INK_MUTED, labelsize=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout(rect=(0, 0.02 if official else 0, 1, max(top_rect, 0.6)))
    if official:
        _official_header(fig, district)
        _official_footer(fig)
    savefig_retry(fig, out_file, dpi=FIG_DPI, facecolor=surface)
    plt.close(fig)
