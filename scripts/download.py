import os
import re
import requests
import pystac_client
import planetary_computer

DATA_DIR = "data"
WORLDPOP_COUNTRIES = [
    "AFG", "AGO", "ALB", "ARE", "ARG", "AUS", "BGD", "BRA", "CAN", "CHN",
    "COD", "COL", "DEU", "EGY", "ETH", "FRA", "GBR", "GHA", "IDN", "IND",
    "IRN", "IRQ", "ITA", "JPN", "KEN", "MEX", "MMR", "MOZ", "MYS", "NGA",
    "PAK", "PER", "PHL", "POL", "RUS", "SAU", "SDN", "THA", "TUR", "TZA",
    "UGA", "UKR", "USA", "VNM", "ZAF", "ZMB", "ZWE",
]

HANSEN_LATS = [f"{d:02d}N" for d in range(0, 90, 10)] + \
              [f"{d:02d}S" for d in range(10, 60, 10)]
HANSEN_LONS = [f"{d:03d}E" for d in range(0, 180, 10)] + \
              [f"{d:03d}W" for d in range(10, 190, 10)]
HANSEN_LAYERS = ["lossyear", "treecover2000", "gain", "datamask"]


def _download_file(url: str, dest: str, chunk_size: int = 8192, session: requests.Session | None = None):
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = (session or requests).get(url, stream=True)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size):
            f.write(chunk)


def download_lulc(years: list[int] | None = None):
    dest_dir = os.path.join(DATA_DIR, "lulc")

    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    search = catalog.search(collections=["io-lulc-annual-v02"])
    items = list(search.items())

    for item in items:
        item_year = int(item.id.split("-")[-1]) if re.search(r"\d{4}", item.id) else None
        if years and item_year not in years:
            continue
        for key, asset in item.assets.items():
            if asset.media_type and "tif" in asset.media_type:
                fname = f"{item.id}_{key}.tif"
                _download_file(asset.href, os.path.join(dest_dir, fname))


def download_osm_roads():
    pass


def download_travel_time():
    dest_dir = os.path.join(DATA_DIR, "travel_time")
    url = (
        "https://data.malariaatlas.org/geoserver/ows"
        "?service=CSW&version=2.0.1&request=DirectDownload"
        "&ResourceId=Explorer:2015_accessibility_to_cities_v1.0"
    )
    _download_file(url, os.path.join(dest_dir, "2015_accessibility_to_cities.tif"))


def download_worldpop(year: int = 2020, countries: list[str] | None = None):
    countries = countries or WORLDPOP_COUNTRIES
    dest_dir = os.path.join(DATA_DIR, "worldpop", str(year))

    base = (
        "https://worldpop-public-data.soton.ac.uk"
        f"/GIS/Population/Global_2015_2030/R2025A/{year}"
    )

    session = requests.Session()
    for iso3 in countries:
        url = f"{base}/{iso3}/v1/100m/unconstrained/{iso3.lower()}_ppp_{year}_unconstrained.tif"
        dest = os.path.join(dest_dir, f"{iso3}_{year}.tif")
        try:
            _download_file(url, dest, session=session)
        except requests.HTTPError:
            pass


def download_nightlights(years: list[int] | None = None):
    years = years or list(range(2012, 2025))
    dest_dir = os.path.join(DATA_DIR, "nightlights")

    token = os.environ.get("BII_EOG_TOKEN")
    if not token:
        return

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    for y in years:
        v = "v22" if y >= 2022 else "v21"
        listing_url = f"https://eogdata.mines.edu/nighttime_light/annual/{v}/{y}/"


def download_forest_management():
    dest_dir = os.path.join(DATA_DIR, "forest_management")
    url = "https://zenodo.org/records/4541513/files/FML_v3-2.tif"
    _download_file(url, os.path.join(dest_dir, "FML_v3-2.tif"))


def download_sdpt():
    dest_dir = os.path.join(DATA_DIR, "sdpt")
    url = "https://gfw-files.s3.amazonaws.com/plantations/SDPT_v2.1/sdpt_v21_v09152024_public.gdb.zip"
    _download_file(url, os.path.join(dest_dir, "sdpt_v21.gdb.zip"))


def download_hansen(layers: list[str] | None = None,
                    lats: list[str] | None = None,
                    lons: list[str] | None = None):
    layers = layers or HANSEN_LAYERS
    lats = lats or HANSEN_LATS
    lons = lons or HANSEN_LONS
    dest_dir = os.path.join(DATA_DIR, "hansen")

    base = "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12"
    session = requests.Session()

    for layer in layers:
        layer_dir = os.path.join(dest_dir, layer)
        for lat in lats:
            for lon in lons:
                fname = f"Hansen_GFC-2024-v1.12_{layer}_{lat}_{lon}.tif"
                url = f"{base}/{fname}"
                dest = os.path.join(layer_dir, fname)
                try:
                    _download_file(url, dest, session=session)
                except requests.HTTPError:
                    pass
