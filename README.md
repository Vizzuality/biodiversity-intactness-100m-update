# Global 100m Projections of Biodiversity Intactness for the years 2017-2024 (v1.1)

Annual global 100 m gridded maps of terrestrial Biodiversity Intactness, extended to 2017–2024.

This is an update of the Biodiversity Intactness product by Impact Observatory and Vizzuality, originally
covering 2017–2020 ([Planetary Computer dataset](https://planetarycomputer.microsoft.com/dataset/io-biodiversity),
[technical whitepaper](https://ai4edatasetspublicassets.blob.core.windows.net/assets/pdfs/io-biodiversity/Biodiversity_Intactness_whitepaper.pdf);
Gassert, Mazzarello & Hyde 2022). The model is unchanged from the whitepaper. This release re-runs it
on updated input datasets refreshing and extending the timeseries to 2017-2024.

## Model

Following [Newbold et al. (2016)](https://doi.org/10.1126/science.aaf2201) and
[Hill et al. (2018)](https://doi.org/10.1101/311787), Biodiversity Intactness is estimated as two
linear-mixed-effects predictors, Abundance and Compositional Similarity, fit to the PREDICTS
database of >32,000 site observations ([Hudson et al. 2016](https://doi.org/10.5519/0066354)/[2017](https://doi.org/10.1002/ece3.2579))
against global spatial proxies for human pressure. Biodiversity Intactness is computed per ~100 m pixel (EPSG:4326) as 
`bii = abundance * community_similarity`.

| input | dataset | citation | per-year | updated |
|---|---|---|---|---|
| land cover | [Sentinel-2 10 m Land Use/Land Cover Time Series, 9-class](https://registry.opendata.aws/io-lulc/) (2017–2024) | Karra et al. 2021 | yes | yes |
| forest loss | [Global Forest Change, v1.12](https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12/download.html) (2000–2024) | Hansen et al. 2013 | yes| yes |
| forest management | [Global Forest Management, v3.2](https://zenodo.org/records/4541513) (2015) | Lesiv et al. 2022 | no | no |
| population | [WorldPop Global Population, constrained 100 m, R2025A](https://hub.worldpop.org/geodata/listing?id=135) (2015–2030) | Bondarenko et al. 2025 | yes | yes |
| nightlights | [VIIRS Nighttime Lights (VNL), v2.1 / v2.2 annual median composites](https://eogdata.mines.edu/products/vnl/) (2017–2024) | Elvidge et al. 2021 | yes | yes |
| accessibility | [Global Map of Travel Time to Cities](https://malariaatlas.org/project-resources/accessibility-to-cities/) (2015) | Weiss et al. 2018 | no | no |
| roads | [OpenStreetMap](https://www.openstreetmap.org/) (2026-06-25) | OpenStreetMap contributors | no | yes |

v1.1 results closely match values for the original timeseries with minor differences due to updated source datasets. 
Most notably, this version uses WorldPop's constrained population estimates, while the original used unconstrained estimates
due to data availability.

## Run
Computation is set up to run on AWS Batch. All input sources are first converted to COGs,
then processing dynamically resamples data to compute final output. This allows previewing
of subsets or reduced resolution sampels prior to the final run.

```sh
uv sync --extra dev
```

Bring up infra and deploy the image: see `infra/README.md` and `scripts/deploy.sh`. The CLIs
read AWS credentials and `BII_BATCH_*` pointers from a `.env`. See `.sample.env`.

```sh
# build local docker image
docker build . -t bii
# downlead and convert source data to cogs for small area in local data dir with docker 
# (will subset when possible but also process some large global files)
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

 - **BII layers:** per-year COGs at `s3://vizz-bii/out/<run_id>/bii_<year>/bii_<year>_<north>_<west>.tif`; public read over HTTP at `https://vizz-bii.s3.amazonaws.com/...`.
 - **Mosaic index:** per-year MosaicJSON (quadkey → overlapping COG URIs) at `s3://vizz-bii/out/<run_id>/bii_<year>/bii_<year>_mosaic.json`.
 - **STAC catalog:** one STAC GeoParquet over every output COG at `s3://vizz-bii/out/<run_id>/catalog.parquet`.

## Notebooks
 - `1. visualize-local.ipynb`: after `test_stage_local.py`, inspect and preview bii.
 - `2. verify-source-coverage.ipynb`: after `stage.py`, inspect cogs correctly generated.
 - `3. visualize-s3.ipynb`: same as `1. visualize-local.ipynb` but sourcing from s3 staged cogs.
 