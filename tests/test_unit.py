"""Unit tests for staging — no network."""

import shutil

import numpy as np
import pytest
import rasterio as rio
from affine import Affine
from rio_cogeo.cogeo import cog_validate

from bii import config, io, tile_index
from bii import cog
from bii.staging import MODULES

_needs_gdal_rasterize = pytest.mark.skipif(
    shutil.which("gdal_rasterize") is None,
    reason="gdal_rasterize CLI not on PATH (required for vector rasterization)",
)
_needs_gdal_clis = pytest.mark.skipif(
    shutil.which("gdal_rasterize") is None or shutil.which("ogr2ogr") is None,
    reason="gdal_rasterize + ogr2ogr CLIs not on PATH (required for reprojecting rasterization)",
)


def _write_geojson(path, geometries) -> str:
    """Write an EPSG:4326 FeatureCollection of ``geometries`` (GeoJSON dicts) to ``path``."""
    import json

    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "properties": {}, "geometry": g} for g in geometries]}
    path.write_text(json.dumps(fc))
    return str(path)


def test_staged_uri_local_override(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STAGED_ROOT", str(tmp_path))
    assert config.staged_uri("forestLoss", "x.tif") == f"{tmp_path}/forestLoss/x.tif"


def test_list_units_shapes():
    for name, module in MODULES.items():
        units = module.list_units()
        assert units, name
        assert all("id" in u for u in units), name


def test_translate_to_cog(local_staged, tmp_path):
    res, n = config.SCALE_DEG, 64
    transform = Affine.translation(-85.0, 9.0 + n * res) * Affine.scale(res, -res)
    src = str(tmp_path / "src.tif")
    with rio.open(src, "w", driver="GTiff", height=n, width=n, count=1,
                  dtype="uint8", crs="EPSG:4326", transform=transform) as ds:
        ds.write(np.ones((n, n), "uint8"), 1)

    dst = config.staged_uri("test", "t.tif")
    assert not io.exists(dst)
    cog.translate_to_cog(src, dst, resampling="nearest")
    assert io.exists(dst)

    valid, errors, _ = cog_validate(dst)
    assert valid, errors
    with rio.open(dst) as out:
        assert out.crs.to_epsg() == 4326
        assert out.dtypes[0] == "uint8"
    fp = cog.footprint(dst, tile_index.INDEX_CRS)
    assert fp[0] == pytest.approx(-85.0, abs=1e-6)
    assert fp[3] == pytest.approx(9.0 + n * res, abs=1e-6)

    cog.translate_to_cog(src, dst, resampling="nearest")
    assert io.exists(dst)


def test_index_build_and_lookup(local_staged):
    footprints = [
        ("s3://b/a.tif", (-90, 0, -80, 10)),
        ("s3://b/b.tif", (-80, 0, -70, 10)),
        ("s3://b/c.tif", (0, 0, 10, 10)),
    ]
    tile_index.build_index("forestLoss", footprints)

    hits = tile_index.lookup("forestLoss", (-81, 1, -79, 2))
    assert set(hits) == {"s3://b/a.tif", "s3://b/b.tif"}

    far = tile_index.lookup("forestLoss", (50, 50, 51, 51))
    assert far == []


def test_index_splits_antimeridian_region(local_staged):
    """Regions straddling the antimeridian (e.g. Fiji) have west > east; pre-fix this inverted
    into a globe-spanning bbox that matched every lookup."""
    fiji = ("s3://b/fiji.tif", (176.8, -20.7, -178.0, -12.5))  # w, s, e, n
    germany = ("s3://b/germany.tif", (5.9, 47.3, 15.0, 55.1))
    tile_index.build_index("roads", [fiji, germany])

    assert tile_index.lookup("roads", (179, -19, -179, -13)) == ["s3://b/fiji.tif"]
    # Same latitude band as Fiji but on the far side of the globe: pre-fix, the inverted bbox
    # covered nearly the whole longitude range at this latitude and matched here too.
    assert tile_index.lookup("roads", (100, -18, 101, -17)) == []


def _write_cog(uri, bounds, n=8):
    """Write an EPSG:4326 raster covering ``bounds`` (w, s, e, n) to ``uri``."""
    transform = rio.transform.from_bounds(*bounds, n, n)
    with io.staged_local_path(uri) as path, rio.open(
        path, "w", driver="GTiff", height=n, width=n, count=1,
        dtype="uint8", crs="EPSG:4326", transform=transform) as ds:
        ds.write(np.ones((n, n), "uint8"), 1)


def test_index_cogs_rebuilds_from_staged_cogs(local_staged):
    alb = config.staged_uri("population", "2020", "ALB_2020.tif")
    _write_cog(alb, (19, 39, 21, 43))
    _write_cog(config.staged_uri("population", "2020", "CRI_2020.tif"), (-86, 8, -82, 11))
    _write_cog(config.staged_uri("population", "2019", "ALB_2019.tif"), (19, 39, 21, 43))

    uri = tile_index.index_cogs("population", year=2020)
    assert io.exists(uri)

    # point in Albania.
    hits = tile_index.lookup("population", (20, 40, 20.5, 40.5), year=2020)
    assert hits == [alb]


def test_lookup_missing_index_returns_empty(local_staged):
    assert tile_index.lookup("forestLoss", (0, 0, 1, 1)) == []


# --------------------------------------------------------------------------------------
# Vector rasterization
# --------------------------------------------------------------------------------------
def test_snap_grid_snaps_to_bii_grid():
    res = config.SCALE_DEG
    w, h, snapped = cog.snap_grid((10.3, 50.1, 10.7, 50.6))
    assert (snapped[0] / res) == pytest.approx(round(snapped[0] / res))
    assert (snapped[1] / res) == pytest.approx(round(snapped[1] / res))
    assert w == round((snapped[2] - snapped[0]) / res)
    assert h == round((snapped[3] - snapped[1]) / res)


@_needs_gdal_rasterize
def test_rasterize_to_cog_polygon_and_line(local_staged, tmp_path):
    bounds = (10.0, 50.0, 10.5, 50.5)
    poly = {"type": "Polygon",
            "coordinates": [[[10.1, 50.1], [10.4, 50.1], [10.4, 50.4], [10.1, 50.4], [10.1, 50.1]]]}
    src = _write_geojson(tmp_path / "poly.geojson", [poly])
    dst = config.staged_uri("test", "poly.tif")
    cog.rasterize_to_cog(src, dst, bounds)

    valid, errors, _ = cog_validate(dst)
    assert valid, errors
    arr = _open_cog_band(dst)
    assert arr.dtype == np.uint8
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.sum() > 0
    fp = cog.footprint(dst, tile_index.INDEX_CRS)
    assert fp[0] <= bounds[0] and fp[3] >= bounds[3]

    # Thin diagonal line must still burn (all_touched=True).
    line = {"type": "LineString", "coordinates": [[10.0, 50.0], [10.5, 50.5]]}
    src2 = _write_geojson(tmp_path / "line.geojson", [line])
    dst2 = config.staged_uri("test", "line.tif")
    cog.rasterize_to_cog(src2, dst2, bounds)
    assert _open_cog_band(dst2).sum() > 0


# --------------------------------------------------------------------------------------
# sdpt / roads enumeration
# --------------------------------------------------------------------------------------
def test_sdpt_units_match_config():
    from bii.staging import sdpt

    units = sdpt.list_units()
    assert len(units) == len(sdpt.REGIONS)
    cri = next(u for u in units if u["id"] == "cri")
    assert cri["layer"] == "cri_plant_v21"
    assert sdpt.ASSET == "forestManagement"  # provider-agnostic asset, swap point with fml


@_needs_gdal_clis
def test_sdpt_stage_unit_reprojects_non_4326_source(local_staged, tmp_path, monkeypatch):
    # ~12% of SDPT country layers are EPSG:3857/UTM. gdal_rasterize burns coordinates onto the
    # degree grid as-is, so without reprojection the meter coordinates miss the grid (empty burn).
    import geopandas as gpd
    from shapely.geometry import box

    from bii.staging import sdpt

    # 10°E,45°N -> EPSG:3857 ~ (1113194, 5621521); +0.3° ~ +33000 / +47000 m.
    gdf = gpd.GeoDataFrame(geometry=[box(1113194, 5621521, 1146600, 5668500)], crs="EPSG:3857")
    src = str(tmp_path / "mercator.gpkg")
    gdf.to_file(src, driver="GPKG", layer="plant")
    monkeypatch.setattr(sdpt, "_source_path", lambda: src)

    assert sdpt.stage_unit({"id": "x", "region": "x", "layer": "plant"}) is True
    dst = sdpt._dst("x")
    valid, errors, _ = cog_validate(dst)
    assert valid, errors
    with rio.open(dst) as r:
        assert r.crs.to_epsg() == 4326
        arr = r.read(1)
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.sum() > 0


@_needs_gdal_clis
def test_sdpt_stage_unit_reads_bounds_from_source(local_staged, tmp_path, monkeypatch):
    # No explicit window: the COG extent is the layer's own bounds (units carry no bounds).
    import geopandas as gpd
    from shapely.geometry import box

    from bii.staging import sdpt

    poly = box(10.0, 45.0, 10.3, 45.3)
    src = str(tmp_path / "wgs84.gpkg")
    gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326").to_file(src, driver="GPKG", layer="plant")
    monkeypatch.setattr(sdpt, "_source_path", lambda: src)

    assert sdpt.stage_unit({"id": "x", "region": "x", "layer": "plant"}) is True
    dst = sdpt._dst("x")
    w, s, e, n = cog.footprint(dst, tile_index.INDEX_CRS)
    assert w <= 10.0 and s <= 45.0 and e >= 10.3 and n >= 45.3
    assert _open_cog_band(dst).sum() > 0


def test_roads_manifest_units():
    from bii.staging import roads

    units = roads.list_units()
    assert len(units) > 100
    u = units[0]
    assert {"id", "url", "bounds"} <= set(u)
    assert u["url"].endswith(".osm.pbf")
    w, s, e, n = u["bounds"]
    assert w < e and s < n
    cx = roads.list_units(regions=["christmas-island"])
    assert len(cx) == 1 and cx[0]["id"] == "christmas-island"
    # Duplicate-extent macro-regions are excluded; their children provide coverage instead.
    ids = {u["id"] for u in units}
    assert roads.GEOFABRIK_DROP_IDS.isdisjoint(ids)
    assert "us/delaware" in ids and "us" not in ids


def test_roads_osmfilter_args_collapse_subtype_drops():
    from bii.staging import roads

    args = roads._osmfilter_args()
    assert args[0] == "--keep=highway="
    assert "--drop=tunnel=yes" in args
    # All sub-type drops collapse into ONE --drop (osmfilter reuses the last key for ` =value`).
    drops = [a for a in args if a.startswith("--drop=highway=")]
    assert drops == ["--drop=highway=" + " =".join(roads.OSM_HIGHWAY_DROP_VALUES)]


def _open_cog_band(uri, band=1):
    with rio.open(uri) as src:
        return src.read(band)
