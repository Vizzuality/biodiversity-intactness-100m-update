# update_bii

Update the global 100 m **Biodiversity Intactness Index (BII)** for 2017–2024.

## Model

BII is computed per ~100 m pixel (EPSG:4326) as `bii = abundance * community_similarity`, two
linear predictors (PREDICTS coefficients in `src/bii/model.py`) of land cover, forest loss/management,
population, nightlights, roads, and accessibility.

| input | source | per-year |
|---|---|---|
| land cover | Impact Observatory 10 m LULC (read in place from STAC) | yes |
| forest loss | Hansen Global Forest Change | filtered per year |
| forest management | Lesiv FML v3.2 or SDPT v2.1 (swappable) | no |
| population | WorldPop 100 m | yes |
| nightlights | VIIRS VNL | yes |
| accessibility | Malaria Atlas travel time | no |
| roads | OSM (Geofabrik) | no |


## Run

```sh
uv sync --extra process --extra dev          # install
```

Bring up infra and deploy the image: see `infra/README.md` and `scripts/deploy.sh`. The CLIs
read AWS credentials and `BII_BATCH_*` pointers from a gitignored `.env` (populated from
`tofu output`).

```sh
# downlead and convert source data to cogs in local data dir with docker 
# (will subset when possible but some large global files)
python scripts/test_stage_local.py --bounds -86 9 -84 11 --year 2020  
# end-to-end test one chunk in local docker
python scripts/test_chunk.py --bounds -86 9 -84 11 --year 2020
```

```sh
# stage input datasets as cogs
python scripts/stage.py --dataset roads --executor docker   # one dataset, locally
python scripts/stage.py --executor batch                    # everything, on Batch
```

```sh
# compute bii from staged cogs
python scripts/run.py --bounds -86 9 -84 11 --executor batch   # a region
python scripts/run.py --executor batch                         # global
```

## Outputs

 - **BII layers:** per-year COGs at `s3://vizz-bii/out/<run_id>/bii_<year>/bii_<year>_<north>_<west>.tif` (`run_id` default `v1_1`); public read over HTTP at `https://vizz-bii.s3.amazonaws.com/out/<run_id>/...`.
 - **Mosaic index:** per-year MosaicJSON (quadkey → overlapping COG URIs) at `s3://vizz-bii/out/<run_id>/bii_<year>/bii_<year>_mosaic.json` (same public HTTP), consumed by `bii_map.html`.
 - **Input footprint indexes:** per-asset GeoParquet (`{geometry, uri}`) at `s3://vizz-bii-processing/input_cogs/<asset>/[<year>/]<asset>_index.parquet`; landcover is indexed in place against the IO STAC.

## Notebooks
 - `1. visualize-local.ipynb`: after `test_stage_local.py`, inspect and preview bii.
 - `2. verify-source-coverage.ipynb`: after `stage.py`, inspect cogs correctly generated.
 - `3. visualize-s3.ipynb`: same as `1. visualize-local.ipynb` but sourcing from s3 staged cogs.
 