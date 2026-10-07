# ERA5-Land Climatology & Drought Monitoring Pipeline

A Python pipeline for downloading ERA5-Land reanalysis data, computing WMO-standard
climate normals and anomalies, and producing a full suite of drought and
agrometeorological indices for any region, any time period, and any combination
of input variables.

## Overview

The pipeline has two layers:

- **`pipeline.py` / `backtest.py`** — the core, place-agnostic data layer: ERA5-Land
  download from the Copernicus Climate Data Store (CDS), unit conversion, hourly-to-daily
  aggregation, boundary clipping to an exact polygon (not just a bounding box), climate
  normals, anomalies, and evapotranspiration (FAO-56 Penman-Monteith).
- **`indices.py` / `run_climatology.py`** — the analysis layer built on top: drought and
  climate index computation (SPI, SPEI, PNI, RAI, PCI, Aridity Index, EDDI, water-balance
  anomaly, an IMD-aligned rainfall departure index, and ETCCDI climate extremes), plus
  research-grade figure generation, exposed as a single generalized CLI.

Nothing in the analysis layer is hardcoded to one location. A run is defined entirely by
its inputs: a region (bounding box or a KML/shapefile/GeoJSON boundary), a date range, a
set of ERA5-Land variables, and a set of indices to compute.

## Features

- **Any region** — bounding box or an exact polygon boundary (KML, shapefile, GeoJSON).
- **Any time period** — an independent baseline period (WMO climate normal, default
  1991-2020) and data period (extendable to the present), so normals stay fixed while
  the analysis window grows.
- **Selectable inputs** — choose which ERA5-Land variables to download and which indices
  to compute; each index is independently gated on the variables it needs, with a
  documented fallback (temperature-only Hargreaves PET) when the full Penman-Monteith
  input set isn't available.
- **Resumable downloads** — CDS requests are deduplicated against what's already on disk,
  with automatic retry/backoff on rate limiting.
- **Research-grade output** — every figure carries its governing formula, a methodology
  note, and a stated baseline period; every NetCDF carries CF-1.8 metadata.

## Indices computed

| Index | Description | Reference |
|---|---|---|
| SPI | Standardized Precipitation Index | WMO-No. 1090 (2012); McKee et al. (1993) |
| SPEI | Standardized Precipitation-Evapotranspiration Index | Water balance (P - PET), z-score standardized |
| PNI | Percent of Normal precipitation Index | — |
| RAI | Rainfall Anomaly Index | Van Rooy (1965) |
| PCI | Precipitation Concentration Index | Oliver (1980) |
| AI | Aridity Index (annual P / annual PET) | UNEP classification |
| EDDI | Evaporative Demand Drought Index | SPI methodology applied to PET |
| Water-balance anomaly | (P - PET) deviation from its monthly normal | — |
| IMD rainfall departure | Percentage departure from normal | India Meteorological Department's published convention |
| ETCCDI extremes | CDD, CWD, R95p, SDII | Expert Team on Climate Change Detection Indices, via `xclim` |

Reference evapotranspiration (PET) uses the FAO-56 Penman-Monteith method (Allen et al.,
1998) when humidity, wind, solar radiation, and surface pressure are available, falling
back to the temperature-only Hargreaves (1985) approximation otherwise.

## Repository structure

```
pipeline.py              Core data layer: download, preprocess, normals, anomalies
backtest.py               ERA5-Land download/aggregation utilities, FAO-56 PET
indices.py                 Drought/climate index math and figure generation (place-agnostic)
chhindwara_indices.py       Reference implementation: full pipeline for Chhindwara district
run_climatology.py          Generalized CLI: any place, any period, any variables/indices
config/defaults.yaml       Variable list, aggregation rules, thresholds
backtest/download_*.py       Example download scripts for specific regions/periods
```

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Register for a free CDS account at https://cds.climate.copernicus.eu, then accept the
   ERA5-Land dataset license at
   https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land (downloads fail
   silently without this).
3. Get your API key from https://cds.climate.copernicus.eu/how-to-api and save it to
   `~/.cdsapirc`:
   ```
   url: https://cds.climate.copernicus.eu/api
   key: <your-personal-access-token>
   ```
   Alternatively, set the `CDSAPI_URL`/`CDSAPI_KEY` environment variables.

## Usage

### Generalized CLI — any place, any period, any indices

```
python run_climatology.py --place "MyTown" --bbox 23.0 78.0 22.0 79.0 \
    --start-year 2014 --end-year 2023 --variables t2m,tp --indices spi,pni
```

Full index set, including Penman-Monteith PET, for an already-downloaded region:

```
python run_climatology.py --place "MyRegion" --boundary-file region.kml \
    --start-year 1991 --end-year 2026 \
    --variables t2m,tp,d2m,u10,v10,ssrd,sp --indices all --skip-download
```

`--indices` accepts any comma-separated subset of
`spi,spei,pni,rai,pci,ai,eddi,water-balance,imd-departure,weekly,fortnightly,etccdi`,
or `all`. Run `python run_climatology.py --help` for the full flag reference.

### Reference implementation

```
python chhindwara_indices.py
```

Runs the full pipeline end to end for Chhindwara district (Madhya Pradesh, India),
1991-2026, against the WMO 1991-2020 climate normal baseline.

## Methodology notes

- SPI follows the WMO Standardized Precipitation Index User Guide (WMO-No. 1090, 2012):
  gamma distribution fit per calendar month, with a separate zero-rainfall probability
  mass handled before fitting.
- The climate normal baseline (1991-2020) is the WMO's current internationally defined
  reference period.
- SPEI is standardized via a per-calendar-month z-score of the water balance, a
  simplified stand-in for the published method's 3-parameter log-logistic fit.
- The IMD rainfall departure index matches India Meteorological Department's published
  category thresholds (Excess/Normal/Deficient/Scanty/No rain), verified against IMD
  Pune's official glossary, but does not necessarily use IMD's own baseline years.

## Known limitations

- SPEI and EDDI use simplified standardization rather than the full published
  distribution-fitting methods.
- PDSI, scPDSI, CMI, and the Palmer Z-Index are not implemented — they require an
  assumed soil water-holding capacity and a multi-step accounting procedure this
  pipeline does not currently have inputs for.
- ERA5-Land is a model reanalysis product (~9km grid), not a direct ground
  observation network — absolute values may differ from station-based products such as
  IMD's own gridded rainfall dataset (~25km grid, gauge-interpolated), even where the
  methodology matches.
