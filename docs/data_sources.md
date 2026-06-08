# Data Sources for Biodiversity Intactness Index (BII)

Data sources used in the whitepaper "Global 100m Projections of Biodiversity Intactness for the years 2017-2020" (Gassert, Mazzarello, Hyde, August 2022), with links to updated versions and raw data download URLs.

---

## 1. 10m Annual Land Use Land Cover (9-class)

**Used for:** `lcCrops_100m`, `lcCrops_1000m`, `lcBuiltArea_100m`, `lcBuiltArea_1000m`

**Original version:** K. Karra et al. 2021 (Impact Observatory / Esri)
**Updated version:** Time series now covers **2017-2024** (v3).

### Download

| Source | URL |
|---|---|
| **AWS S3 bucket** | `s3://io-10m-annual-lulc/` (region: `us-west-2`, no auth required) |
| **STAC API** | https://api.impactobservatory.com/stac-aws/collections/io-10m-annual-lulc/items |
| **Planetary Computer STAC** | https://planetarycomputer.microsoft.com/api/stac/v1/collections/io-lulc-annual-v02 |
| **AWS Open Data Registry** | https://registry.opendata.aws/io-lulc/ |
| **IO Store (free download)** | https://www.impactobservatory.com/10m-land-cover/ |
| **Esri Living Atlas Explorer** | https://livingatlas.arcgis.com/landcoverexplorer/ |

Tiles are aligned to the ESA Sentinel-2 UTM tiling grid (~733 tiles per year, ~5,100 tiles total across 2017-2023). All tiles are served as **Cloud-Optimized GeoTIFFs (COGs)**, so they can be read directly via HTTP range requests without downloading the full files. This is the preferred access method — avoid bulk downloading the raw data (~100 MB per tile, ~500 GB total).

Explore with:
```bash
aws s3 ls --no-sign-request s3://io-10m-annual-lulc/
```

---

## 2. OpenStreetMap (Distance to Roads)

**Used for:** `ln(distRoads + 1)`

**Original version:** OSM Planet Dump 2021
**Updated version:** Planet dumps updated weekly.

### Download

| Source | URL |
|---|---|
| **Planet PBF (latest)** | https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf |
| **Planet PBF (dated)** | `https://planet.openstreetmap.org/pbf/planet-YYMMDD.osm.pbf` |
| **Torrent** | https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf.torrent |
| **AWS S3 (EU)** | `s3://osm-planet-eu-central-1/` (no auth required) |
| **AWS S3 (US)** | `s3://osm-planet-us-west-2/` (no auth required) |
| **Geofabrik regional extracts** | https://download.geofabrik.de/ |

Filter roads from the planet file:
```bash
osmium tags-filter planet-latest.osm.pbf w/highway -o global-roads.osm.pbf
```

**Alternative: GRIP v4 Global Roads** (pre-processed global roads vector dataset):
- Download page: https://www.globio.info/download-grip-dataset
- UNDP GeoHub: https://geohub.data.undp.org/data/300da70781b7a53808aab824543e6c2b

---

## 3. Oxford MAP Travel Time to Cities

**Used for:** `ln(accessibility + 1)`

**Original version:** Weiss et al. 2018 (2015 accessibility surface)
**Updated version:** 2020 friction surface available; no updated travel-time-to-cities raster beyond 2015. The 2020 friction surface can be used to regenerate travel-time-to-cities.

### Download

| File | Download URL |
|---|---|
| **2015 travel time to cities** | https://data.malariaatlas.org/geoserver/ows?service=CSW&version=2.0.1&request=DirectDownload&ResourceId=Explorer:2015_accessibility_to_cities_v1.0 |
| **2015 friction surface** | https://data.malariaatlas.org/geoserver/ows?service=CSW&version=2.0.1&request=DirectDownload&ResourceId=Explorer:2015_friction_surface_v1_Decompressed |
| **2020 motorized friction surface** | https://data.malariaatlas.org/geoserver/ows?service=CSW&version=2.0.1&request=DirectDownload&ResourceId=Explorer:2020_motorized_friction_surface |
| **2020 walking-only friction surface** | https://data.malariaatlas.org/geoserver/ows?service=CSW&version=2.0.1&request=DirectDownload&ResourceId=Explorer:2020_walking_only_friction_surface |
| **2020 motorized travel time to healthcare** | https://data.malariaatlas.org/geoserver/ows?service=CSW&version=2.0.1&request=DirectDownload&ResourceId=Explorer:2020_motorized_travel_time_to_healthcare |
| **2020 walking-only travel time to healthcare** | https://data.malariaatlas.org/geoserver/ows?service=CSW&version=2.0.1&request=DirectDownload&ResourceId=Explorer:2020_walking_only_travel_time_to_healthcare |
| **Figshare (travel time to cities + ports)** | https://figshare.com/articles/dataset/Travel_time_to_cities_and_ports_in_the_year_2015/7638134/3 |
| **R accessibility mapping script** | https://malariaatlas.org/wp-content/uploads/2022/11/R_generic_accessibilty_mapping_script_2020-1.txt |
| **MAP project page** | https://malariaatlas.org/project-resources/accessibility-to-healthcare/ |
| **R package** | https://github.com/malaria-atlas-project/malariaAtlas |

---

## 4. WorldPop 100m Population Counts

**Used for:** `ln(pD2006_1000m + 1)`

**Original version:** WorldPop et al. 2018 (unconstrained)
**Updated version:** **R2025A v1**, unconstrained 100m, annual **2000-2030**.

### Download

Two dataset families exist:

**Global_2000_2020 (older, used in whitepaper):**
| File | URL pattern |
|---|---|
| **Country 100m GeoTIFF** | `https://data.worldpop.org/GIS/Population/Global_2000_2020/{YEAR}/{ISO3}/{iso3}_ppp_{YEAR}.tif` |
| **Global 1km mosaic** | `https://data.worldpop.org/GIS/Population/Global_2000_2020/{YEAR}/0_Mosaicked/ppp_{YEAR}_1km_Aggregated.tif` |

**Global_2015_2030 (newer R2025A):**
| File | URL pattern |
|---|---|
| **Country 100m unconstrained** | `https://worldpop-public-data.soton.ac.uk/GIS/Population/Global_2015_2030/R2025A/{YEAR}/{ISO3}/v1/100m/unconstrained/` |
| **Global 1km mosaics** | https://hub.worldpop.org/geodata/listing?id=137 |
| **Hub listing (all countries)** | https://hub.worldpop.org/geodata/listing?id=135 |
| **Dataset CSV index** | https://data.worldpop.org/assets/wpgpDatasets.csv |
| **STAC API** | https://sdi.worldpop.org/ |
| **Release notes (R2025A)** | https://data.worldpop.org/repo/prj/Global_2015_2030/R2025A/doc/Global2_Release_Statement_R2025A_v1.pdf |

---

## 5. VIIRS Nighttime Lights (Annual Composites)

**Used for:** `ln(nL2012_1000m + 1)`

**Original version:** Elvidge et al. 2021, Version 2
**Updated version:** **VNL v2.2**, covering **2012-2024**.

### Download

| Source | URL |
|---|---|
| **EOG product page** | https://eogdata.mines.edu/products/vnl/ |
| **Annual v2.2 directory** | https://eogdata.mines.edu/nighttime_light/annual/v22/ |
| **Annual v2.1 directory** | https://eogdata.mines.edu/nighttime_light/annual/v21/ |

**Note:** EOG now requires a free account login to download. Register at https://eogdata.mines.edu/.

File naming convention:
```
VNL_v2_npp_{YEAR}_global_vcmslcfg_c{TIMESTAMP}.{BAND}.tif.gz
```
Where `{BAND}` is one of: `average`, `average_masked`, `median`, `median_masked`, `minimum`, `maximum`, `cf_cvg` (cloud-free observations).

The whitepaper uses the **median** annual composite.

---

## 6. Global Forest Management Data (Lesiv et al.)

**Used for:** `managedForest_100m`

**Original version:** Lesiv et al. 2022 (year 2015, 100m)
**Updated version:** No update beyond 2015.

### Download

| File | Download URL |
|---|---|
| **FML_v3-2.tif** (forest management map) | https://zenodo.org/records/4541513/files/FML_v3-2.tif |
| **Class probability GeoTIFF** | https://zenodo.org/records/4541513/files/ProbaV_LC100_epoch2015_global_v2.0.3_forest-management--layer-proba_EPSG-4326.tif |
| **Reference data** | https://zenodo.org/records/4541513/files/reference_data_set.csv |
| **Validation data** | https://zenodo.org/records/4541513/files/validation_data_set.csv |
| **Region-specific models** | https://zenodo.org/records/5849150 (training data `.csv`, parameters `.ini`, models `.joblib.z` per biome) |
| **Zenodo API (file listing)** | https://zenodo.org/api/records/4541513/files |

---

## 7. Spatial Database of Planted Trees (SDPT v2.0/v2.1)

**Alternative to:** Lesiv et al. forest management layer

**Description:** Maps planted forests and tree crops across 158 countries at 30m resolution (Harris et al., WRI/GFW). More recently updated; used by the SBTN Natural Lands Map v1.1. SDPT v2.1 adds data for Brazil, Cambodia, Cote d'Ivoire, Ghana.

### Download

| Source | URL |
|---|---|
| **SDPT v2.0 (zip)** | https://gfw2-data.s3.amazonaws.com/plantations/sdpt/sdpt_v2.zip |
| **SDPT v2.1 (file geodatabase)** | https://gfw-files.s3.amazonaws.com/plantations/SDPT_v2.1/sdpt_v21_v09152024_public.gdb.zip |
| **GFW Data API** (requires free API key) | `https://data-api.globalforestwatch.org/dataset/gfw_planted_forests/{VERSION}/download/gpkg` |
| **GFW Open Data Portal** | https://data.globalforestwatch.org/datasets/planted-forests |
| **WRI technical documentation** | https://www.wri.org/research/spatial-database-planted-trees-sdpt-version-2 |
| **SBTN Natural Lands Map (uses SDPT v2)** | GeoTIFFs available at https://landcarbonlab.org/data/natural-lands-map/ |
| **SBTN NLM technical docs (v1.1)** | https://sciencebasedtargetsnetwork.org/wp-content/uploads/2025/02/Technical-Guidance-2025-Step3-Land-v1_1-Natural-Lands-Map.pdf |
| **SBTN NLM code** | https://github.com/wri/natural-lands-map |

---

## 8. Hansen Global Forest Change

**Used for:** `forestLoss2006_100m`

**Original version:** Hansen et al. 2013, v1.9
**Updated version:** **v1.12**, tree cover loss **2001-2024**.

### Download

| Source | URL |
|---|---|
| **Download page (v1.12)** | https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12/download.html |
| **GLAD UMD project page** | https://glad.umd.edu/dataset/global-forest-change |

**Tile URL pattern:**
```
https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12/Hansen_GFC-2024-v1.12_{LAYER}_{LAT}_{LON}.tif
```

Where:
- `{LAYER}`: `treecover2000`, `lossyear`, `gain`, `datamask`, `first`, `last`
- `{LAT}`: upper-left corner latitude, e.g. `40N`, `00N`, `10S`
- `{LON}`: upper-left corner longitude, e.g. `080W`, `170E`

**Example tiles:**
```
https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12/Hansen_GFC-2024-v1.12_lossyear_40N_080W.tif
https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12/Hansen_GFC-2024-v1.12_treecover2000_10S_050W.tif
```

The `lossyear` band encodes year of loss: 1-24 = years 2001-2024, 0 = no loss. Tiles are 10x10 degrees, 30m resolution, unsigned 8-bit GeoTIFF.

---

## Download Plan

Selected sources for the BII update:

| # | Dataset | Source | Download URL / Method | Notes |
|---|---------|--------|----------------------|-------|
| 1 | 10m Land Cover | Planetary Computer STAC | `https://planetarycomputer.microsoft.com/api/stac/v1/collections/io-lulc-annual-v02` | COG format — read remotely via HTTP range requests, no bulk download needed |
| 2 | OSM Roads | Overpass API | Query with road filtering at download time | Notes in separate repo |
| 3 | Travel Time to Cities | Malaria Atlas direct download | `https://data.malariaatlas.org/geoserver/ows?service=CSW&version=2.0.1&request=DirectDownload&ResourceId=Explorer:2015_accessibility_to_cities_v1.0` | 2015 surface |
| 4 | WorldPop Population | R2025A country 100m unconstrained | `https://worldpop-public-data.soton.ac.uk/GIS/Population/Global_2015_2030/R2025A/{YEAR}/{ISO3}/v1/100m/unconstrained/` | |
| 5 | VIIRS Nighttime Lights | EOG annual directories | v2.2: `https://eogdata.mines.edu/nighttime_light/annual/v22/` / v2.1: `https://eogdata.mines.edu/nighttime_light/annual/v21/` | v2.1 for years < 2022, v2.2 for 2022+. Requires login credentials. |
| 6 | Forest Management | Zenodo direct download | `https://zenodo.org/records/4541513/files/FML_v3-2.tif` | |
| 7 | Planted Trees (SDPT) | S3 direct download | `https://gfw-files.s3.amazonaws.com/plantations/SDPT_v2.1/sdpt_v21_v09152024_public.gdb.zip` | v2.1 file geodatabase |
| 8 | Hansen Forest Change | GCS tile URLs (v1.12) | `https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12/Hansen_GFC-2024-v1.12_{LAYER}_{LAT}_{LON}.tif` | |

---

## Summary of Update Status

| # | Dataset | Original Version | Latest Version | Years Available | Update Frequency |
|---|---------|-----------------|----------------|-----------------|-----------------|
| 1 | 10m Land Cover | 2017-2020 | 2017-2024 (v3) | 2017-2024 | Annual |
| 2 | OpenStreetMap | 2021 dump | Weekly dumps | Continuous | Weekly |
| 3 | Travel Time to Cities | 2015 surface | 2020 friction surface | 2015, 2020 | Irregular |
| 4 | WorldPop Population | 2000-2020 | R2025A v1 (2015-2030) | 2000-2030 | Annual |
| 5 | VIIRS Nighttime Lights | v2 (2012-2019) | v2.2 (2012-2024) | 2012-2024 | Annual |
| 6 | Forest Management (Lesiv) | 2015 (v3.2) | 2015 (v3.2) | 2015 only | No updates |
| 7 | Planted Trees (SDPT) | N/A | v2.1 (WRI/GFW) | ~2020 | Minor updates |
| 8 | Hansen Forest Change | v1.9 (2001-2021) | v1.12 (2001-2024) | 2001-2024 | Annual |
