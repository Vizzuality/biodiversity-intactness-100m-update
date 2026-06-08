# Plan: Serverless chunked BII processing on AWS

## Context

This repo updates the global 100m **Biodiversity Intactness Index (BII)**. The original
working implementation lives in `notebooks/2. biodiversity-impact.ipynb` and
`notebooks/3. pc-hub-execute-bii.ipynb`: it uses `cog_worker` (Vizzuality's rio-tiler
wrapper) to compute BII per chunk, reading inputs from a **private Impact Observatory
STAC API** and running on **Dask via Planetary Computer Hub (Azure)**, writing per-chunk
COGs to Azure blob storage.

We are re-homing this onto **AWS S3 + AWS Batch (EC2 Spot)**, split into the four parts the
user described — staging, processing, a single-chunk local test, and an orchestrator — with
these guiding principles from the user:

- **Avoid moving data where possible.** Inputs that are already cloud-optimized and
  remotely readable are read in place: **LULC reads directly from the Impact Observatory
  STAC** at processing time and is never staged. Staging **stream-converts** other
  sources from their URLs (`/vsicurl`, `/vsigzip`, `/vsizip`) into COGs in S3 — no bulk
  download to local `data/`. `scripts/download.py` stays only as an optional dev helper.
- **Keep source data as tiles**, not heavy global mosaics; a per-asset **footprint index**
  tells each chunk which tiles overlap it (the centerpiece replacement for the STAC API).
- **Staging also runs on Batch** (same container image), as one job per asset/year/tile.
- **SDPT v2.1 is included as a swappable `forestManagement` provider** alongside Lesiv FML
  v3.2, so we can test replacing FML and compare BII outputs.

**Target grid (default, from original):** EPSG:4326, scale = `100/111319.49` deg (~100m),
`buffer = round(10000/100) = 100` px. The 100px buffer + clipping `distRoads` to 10 km makes
the focal ops (`scipy.ndimage.uniform_filter`) and the `mahotas` distance transform safe
across chunk edges — no global passes needed.

## Decisions

- **BII formula:** product — `bii = abundance * community_similarity` (notebook 2; standard
  PREDICTS BII). Notebook 3's sum form is not used.
- **Year range:** **2017–2024** (configurable).
- **Roads:** single-epoch (one recent snapshot reused across all years); not time-varying.

## Architecture — installable `bii/` package + thin CLIs

```
src/bii/
  __init__.py
  config.py            # S3 bucket/prefixes, grid params (proj/scale/buffer), run ids
  model.py             # ported BII math: coefficients, transforms, convolve, distance, calc_bii, compute_all
  sources.py           # asset registry: per-asset read strategy + forestManagement provider switch (FML | SDPT)
  tile_index.py        # footprint-index build + query; unifies staged-GeoParquet AND live-STAC (LULC) lookup
  process.py           # process(chunk_dict) worker entrypoint -> write output COGs to S3
  orchestrate.py       # manifest -> Batch array submit -> verify -> retry-missing (for processing AND staging)
  staging/
    __init__.py
    cog.py             # shared stream-to-COG writer + footprint-index builder (rio-cogeo / rasterio COG driver)
    hansen.py          # forestLoss: re-COG GCS 10° tiles -> S3 (stream via /vsicurl)
    worldpop.py        # population: per-country GeoTIFF -> COG per year
    nightlights.py     # VNL: /vsigzip global -> COG per year
    travel_time.py     # accessibility: global -> COG
    forest_management.py  # FML v3.2: global -> normalized managed-forest COG
    sdpt.py            # SDPT v2.1: rasterize gdb -> normalized managed-forest COG tiles
    roads.py           # OSM: rasterize highways (osmctools) -> per-region 100m COG + index
scripts/
  download.py          # existing — optional dev-only local fetch (de-emphasized)
  stage.py             # thin CLI -> bii.staging (run locally or submit to Batch)
  test_chunk.py        # run ONE chunk end-to-end locally
  run.py               # thin CLI -> bii.orchestrate (submit + verify global processing run)
Dockerfile             # geo base (GDAL/rasterio) + deps; staging, processing, local test
Dockerfile.roads       # osmctools base + gdal-bin: OSM filter + rasterize only
```

`pyproject.toml`: switch to `src/` layout (`[tool.setuptools] packages` / hatchling), add
`cog_worker`, `mahotas`, `rio-tiler`, `rio-cogeo`, `boto3`, plus existing geopandas/rasterio/
pystac-client/planetary-computer.

## 1. Staging — `bii/staging/*` (run on Batch)

Each staging module **streams from the source URL** and writes valid COGs
(`s3://<bucket>/staged/<asset>[/<year>]/...`, internal tiling + `overview_resampling='average'`)
via the shared `cog.py` helper, then registers tile footprints into a GeoParquet index
(`.../<asset>[_<year>]_index.parquet`, columns `geometry` (EPSG:4326) + `uri`). Skips assets
already in S3 (port `_download_file`'s skip-if-exists idiom). Each module exposes a function
the orchestrator can fan out as Batch jobs (one per tile/country/year).

| asset | source | staging |
|---|---|---|
| `landcover` | Impact Observatory STAC `io-10m-annual-lulc` (`api.impactobservatory.com/stac-aws`) | **none — read directly at processing time** |
| `forestLoss` (`lossyear`) | Hansen GCS 10° 30m tiles | stream `/vsicurl` -> COG -> S3 + index |
| `population` | WorldPop per-country 100m | stream -> COG per year -> S3 + index |
| `nightlights` | VNL `.tif.gz` | `/vsigzip` -> COG per year -> S3 (1 entry) |
| `accessibility` | Malaria Atlas travel time | stream -> COG -> S3 (1 entry) |
| `forestManagement` (FML) | Lesiv `FML_v3-2.tif` | -> normalized managed-forest mask COG -> S3 |
| `forestManagement` (SDPT) | SDPT v2.1 `.gdb.zip` | `/vsizip//vsicurl` rasterize to 100m planted mask -> COG tiles + index |
| `roads` | OSM Geofabrik regions (osmctools highway filter) | rasterize to 100m -> per-region COG + index (single-epoch) |

Notes:
- **Vector sources (SDPT, OSM)** can't be pure-streamed; their one-time rasterization runs
  as a Batch staging job using ephemeral disk (acceptable — not a local bulk download).
- **`roads` runs in its own image (`Dockerfile.roads`)**, adapted from the `rasterize-osm`
  POC: download per Geofabrik child region (vendored `geofabrik-index-v1-child.geojson` as the
  fan-out manifest, one array index per region), `osmconvert`/`osmfilter` highway filter,
  `gdal_rasterize -burn 1` at the BII grid (`EPSG:4326`, `100/111319.49`°), translate to COG,
  register footprint. osmctools is much faster than osmium but awkward to build into the
  python-gdal base, hence the dedicated image. Per-region COGs (variable extent) are mosaicked
  on the fly by cog_worker during the windowed chunk read; the footprint index handles overlap.
  Single-epoch — one recent snapshot reused across all years.
- **forestManagement normalization:** both FML and SDPT are staged into a comparable
  **managed-forest mask/fraction** so the model is provider-agnostic. The notebook's FML
  decode (`>30 & <55`) moves into `staging/forest_management.py`; `staging/sdpt.py` emits a
  planted-tree mask. `sources.py` selects which provider feeds `forestManagement`.
- `scripts/download.py:download_osm_roads()` (currently a stub) is superseded by
  `staging/roads.py`.

## 2. Tile lookup — `bii/tile_index.py` (centerpiece, replaces `read_stac`)

- `build_index(asset, footprints, year=None)` — write `{geometry, uri}` GeoParquet (used by staging).
- `lookup(asset, bounds, year=None) -> list[str]` — two backends behind one interface:
  - **staged assets:** read the cached GeoParquet, spatial-query via geopandas `.sindex` for
    footprints intersecting `bounds`, return S3 URIs.
  - **`landcover`:** live STAC search against the Impact Observatory collection
    `io-10m-annual-lulc` (`https://api.impactobservatory.com/stac-aws/collections/io-10m-annual-lulc`)
    for the chunk bbox/year — mirrors the original `read_stac` flow, no staging. (IO STAC is
    used instead of Planetary Computer's `io-lulc-annual-v02`, which only covers through 2023;
    IO covers 2017–2024 and is AWS-hosted.)
- Processing then does `worker.read(lookup(asset, worker.lnglat_bounds(), year))`; cog_worker
  mosaics overlapping tiles on the fly.

## 3. Model — `bii/model.py`

Port verbatim from `notebooks/2`/`3`: `ABUNDANCE_COEFFICIENTS`,
`COMMUNITY_SIMILARITY_COEFFICIENTS`, `INVERSE_TRANSFORMS`, `nominal_scale`, `convolve`,
`fast_distance_transform` (mahotas), `calc_bii`, `compute_all`. Changes:
- Asset acquisition goes through `tile_index.lookup` / `sources.py` instead of STAC helpers.
- `forestManagement` consumes the normalized managed-forest mask from the source adapter
  (provider-agnostic) rather than FML-coded values.
- Apply product-form BII (`abundance * community_similarity`). Keep masking, `distRoads`
  10 km clip, DEG2METERS.

## 4. Worker entrypoint — `bii/process.py`

- `process(chunk: dict)` — rebuild `Worker`/`Manager` from a `chunk_params()` dict, run
  `compute_all`, write each layer as a COG to deterministic key
  `s3://<bucket>/out/<run_id>/<key>/<key>_<north>_<west>.tif` (port `persist_cog`: rasterio
  `MemoryFile`, `driver='COG'`, boto3 upload). Idempotent: skip if key exists.
- Batch entry: `AWS_BATCH_JOB_ARRAY_INDEX` -> line N of S3 `chunks.jsonl`; same `process()`
  runs locally from a dict. Wrap reads in `rio.Env(VSI_CACHE='FALSE', HTTP_MAX_RETRY=3, ...)`.

## 5. Single-chunk local test — `scripts/test_chunk.py`

Manager over original test bounds `(-86, 9, -84, 11)` (Costa Rica), take first chunk from
`chunk_params()`, run `process(chunk)` to a local dir / test S3 prefix, assert output COG is
valid (openable, expected shape, finite). Identical code path to Batch — the gate before fan-out.

## 6. Orchestrator — `bii/orchestrate.py` + `scripts/run.py`

Ports `_cog_worker_run` track/retry to Batch; reused for both staging and processing fan-out:
1. **Manifest:** iterate `manager.chunk_params(chunksize)`, drop non-finite bounds, and drop
   ocean chunks by intersecting the `landcover`/`roads` footprint coverage (skips ~60% of globe).
   Write `chunks.jsonl` to S3.
2. **Submit:** one Batch **array job** (size N). `chunksize=4096` -> ~4,700 chunks (one array,
   under 10k cap); `2048` -> ~18,600 (two arrays / pack ~2 per index). `retryStrategy` for Spot.
3. **Verify + retry:** list S3 `out/<run_id>/...`, diff vs manifest; **missing keys = retry set**
   -> resubmit only those indices; loop until empty.

## Dockerfile(s)

- **`Dockerfile`** — base on a GDAL/rasterio image (`osgeo/gdal` or `ghcr.io/lambgeo/...`),
  `pip install -e .` + deps. One image for processing jobs, the local test, and all
  *raster* staging (Hansen, WorldPop, VNL, travel time, FML, SDPT).
- **`Dockerfile.roads`** — separate image for OSM roads only: `FROM ramunasd/osmctools`
  (or equiv) + `apt install gdal-bin`, then layer the `bii` package. Keeps the proven fast
  osmctools filter pipeline rather than compiling osmctools into the python-gdal base.

## Verification

1. **Local single chunk:** `python scripts/test_chunk.py` — Costa Rica chunk end-to-end;
   assert valid COG; visually compare to the notebook's `bii_2020` preview.
2. **Staging spot-check:** open a few staged COGs + indexes; confirm `tile_index.lookup`
   returns correct tiles for a known bbox (incl. live LULC STAC lookup).
3. **FML vs SDPT:** run a small region with each `forestManagement` provider; compare BII.
4. **Small-region fan-out:** `scripts/run.py` over Costa Rica on Batch; confirm manifest size,
   all outputs present, retry loop converges to zero.
5. **Global run:** orchestrate full extent at `chunksize=4096`, 2017–2024; completion diff empty.
```
