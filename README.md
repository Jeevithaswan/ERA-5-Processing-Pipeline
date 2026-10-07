# ERA5 Processing Pipeline

Downloads gridded ERA5-Land reanalysis data from the Copernicus Climate Data
Store (CDS), preprocesses it to daily NetCDF, computes agrometeorological
indicators, climatic normals, anomalies, climate indices, and PNG charts.

**Everything lives in one file: `pipeline.py`.** Settings that rarely change
(variable list, aggregation rules, normals baseline, indices list, thresholds)
live in `config/defaults.yaml`.

**Time period, location, and variables are runtime inputs, not hardcoded.**
Nothing in the code is specific to one region — it was tested against the
Pandhurna subdistrict boundary (`Pandhurna.kml`) but works for any boundary
file or bounding box you pass in.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Register for a free CDS account at https://cds.climate.copernicus.eu,
   then accept the ERA5-Land dataset license at
   https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land
   (**required** — downloads fail silently without this).
3. Get your API key from https://cds.climate.copernicus.eu/how-to-api
   (while logged in) and save it to `%USERPROFILE%\.cdsapirc` (plain text,
   **no** BOM — use `notepad` or a plain editor, not PowerShell's `Out-File`,
   which adds one and breaks the CDS client):
   ```
   url: https://cds.climate.copernicus.eu/api
   key: <your-personal-access-token>
   ```

## Running

### Interactive mode (recommended if you're not comfortable with flags)

```
python pipeline.py
```
Walks you through every choice — stage, location (boundary file or typed
lat/long box), variables, years, normals baseline, indices — one question
at a time, shows a summary, then asks you to confirm before running.

### Flag mode (for scripting / repeat runs)

```
python pipeline.py download   --boundary Pandhurna.kml --start 2023 --end 2023
python pipeline.py preprocess --boundary Pandhurna.kml --start 2023 --end 2023
python pipeline.py agromet    --boundary Pandhurna.kml --start 2023 --end 2023
python pipeline.py normals    --boundary Pandhurna.kml --normals-start 2023 --normals-end 2023
python pipeline.py anomalies  --boundary Pandhurna.kml --start 2023 --end 2023 --normals-start 2023 --normals-end 2023
python pipeline.py indices    --boundary Pandhurna.kml --start 2023 --end 2023 --normals-start 2023 --normals-end 2023
python pipeline.py plots      --boundary Pandhurna.kml --start 2023 --end 2023
```
Or all seven stages in order:
```
python pipeline.py all --boundary Pandhurna.kml --start 2023 --end 2023 --normals-start 2023 --normals-end 2023
```

A plain bounding box instead of a boundary file (no file needed), custom
variables, custom parallelism:
```
python pipeline.py download --bbox 21.3 78.9 20.9 79.3 --region-name nagpur \
    --start 2024 --end 2024 --variables t2m,tp --parallel 4
```

Location is **either** `--boundary <file>` (any format geopandas reads —
`.shp`, `.geojson`, `.kml`) **or** `--bbox NORTH WEST SOUTH EAST` (degrees).

Full flag reference for any stage: `python pipeline.py <stage> --help`.

## Pipeline stages

1. **download** — pulls hourly ERA5-Land NetCDF from CDS for the requested
   area, one file per variable/year/month, into `data/<region>/raw/<var>/`.
   Resumable (skips existing files) and parallelized (default 4 concurrent
   requests — each spends 1-2 min queued server-side, so parallelism cuts
   wall-clock time roughly proportionally).
2. **preprocess** — converts units (K→°C, Pa→hPa, m→mm), aggregates hourly
   to daily mean/max/min/sum, and — if a `--boundary` file was given —
   masks grid cells outside the actual polygon to NaN (not just its bounding
   box), into `data/<region>/processed/<var>/`.
3. **agromet** — merges t2m/d2m/tp/u10/v10 at hourly resolution to compute
   relative humidity, wind speed, leaf-wetness-duration hours, and
   wind-driven-rain hours (pest/disease-relevant indicators, generalized
   from a point-based sibling pipeline), into `data/<region>/processed/agromet/`.
4. **normals** — day-of-year climatic normals over the baseline period
   (default 1991–2020, WMO standard), 5-day smoothed, into `data/<region>/normals/`.
5. **anomalies** — daily value minus the matching day-of-year normal; for
   precipitation also percent-of-normal, into `data/<region>/anomalies/<var>/`.
6. **indices** — 16 annual indices into `data/<region>/indices/`: 11 generic
   ETCCDI/ECA&D climate indices via `xclim` (frost days, summer days,
   Rx1day/Rx5day, CDD/CWD, TX90p/TN10p, SDII, R95p) plus 5 agromet indices
   (rain days, leaf-wetness hours, wind-driven-rain hours, longest wet/dry
   spell).
7. **plots** — PNG charts into `data/<region>/plots/`: temperature and
   rainfall time series, two index maps, and an all-indices summary
   table/CSV. For showing results to people who don't want to open NetCDF.

Outputs land under `data/<region-name>/{raw,processed,normals,anomalies,indices,plots}/`
— `<region-name>` defaults to the boundary file's stem (`Pandhurna.kml` →
`data/Pandhurna/...`), or pass `--region-name` explicitly.

## Known real bugs already found and fixed (worth knowing before extending this)

- **Precipitation was ~14x too high at first.** ERA5-Land's
  `total_precipitation` is a cumulative value that resets once per day
  (confirmed against real data), not independent hourly amounts — summing
  raw hourly readings massively over-counts. Fixed by reconstructing true
  hourly deltas (`deaccumulate()` in `pipeline.py`) before summing.
- **Agromet counts outside the boundary showed `-9223372036854775808`**
  instead of blank, for integer-typed fields (rain-day flags, hour counts).
  Integers have no NaN to mask clipped-out cells with, so rioxarray filled
  them with a dtype-min sentinel. Fixed by casting to float before clipping
  (`clip_to_polygon()`).
- Both are covered by comments at the fix site — read them before changing
  precipitation handling or boundary clipping.

## Other notes

- **CDS returns a zip disguised as `.nc`** for some requests even with
  `format=netcdf` — `pipeline.py` detects this by content and unwraps it
  automatically (`_unwrap_if_zipped`).
- **xclim API drift**: each of the 11 xclim-based indicators is wrapped in
  a try/except and logged individually rather than crashing the whole run,
  since exact xclim function signatures can change between versions. Check
  `data/<region>/logs/indices.log` if an index doesn't produce output.
- **PROJ/GDAL conflict**: a local PostGIS install can set a system-wide
  `PROJ_LIB` env var that shadows the PROJ database bundled with
  `pyproj`/`rioxarray` and breaks boundary reading. `pipeline.py` clears
  that env var for this process only (not your system-wide setting) before
  reading a boundary file.
- **Percentile-based indices (tx90p, tn10p) need a real multi-year
  baseline** to mean anything — with `--normals-start`/`--normals-end` set
  to a single test year they'll read 0, which is expected, not a bug.
- Research-grade metadata: every NetCDF file carries CF-1.8 attributes
  (`standard_name`, `units`, `source`, `references`, `history`) — see the
  `stamp()` function and `VAR_ATTRS` dict.

## Tested so far

- Full pipeline run against real Pandhurna 2023 data (all 7 stages), with
  physically sanity-checked output (seasonal temperature curve, correct
  monsoon rainfall timing and magnitude, zero frost days as expected for
  the region).
- Interactive mode, end to end.
- Not yet run: the full 1991–2020 normals baseline (needed for real,
  non-placeholder normals/anomalies/percentile indices) — this is a much
  larger download than the one-year test.
