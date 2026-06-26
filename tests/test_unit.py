"""Unit tests for staging — no network. Exercise config, the COG writer, and the index."""

import shutil

import numpy as np
import pytest
import rasterio as rio
from affine import Affine
from rio_cogeo.cogeo import cog_validate

from bii import config, s3io, tile_index
from bii.staging import MODULES, cog

# Vector rasterization shells out to the gdal_rasterize CLI; skip those tests where it's absent.
_needs_gdal_rasterize = pytest.mark.skipif(
    shutil.which("gdal_rasterize") is None,
    reason="gdal_rasterize CLI not on PATH (required for vector rasterization)",
)
# Reprojecting a non-EPSG:4326 source to the grid also shells out to ogr2ogr.
_needs_gdal_clis = pytest.mark.skipif(
    shutil.which("gdal_rasterize") is None or shutil.which("ogr2ogr") is None,
    reason="gdal_rasterize + ogr2ogr CLIs not on PATH (required for reprojecting rasterization)",
)


def _write_geojson(path, geometries) -> str:
    """Write a minimal EPSG:4326 FeatureCollection of ``geometries`` (GeoJSON dicts) to ``path``."""
    import json

    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "properties": {}, "geometry": g} for g in geometries]}
    path.write_text(json.dumps(fc))
    return str(path)


def test_grid_params():
    assert config.BUFFER == 100
    assert config.SCALE_DEG == pytest.approx(100 / config.DEG2METERS)
    assert config.SCALE_DEG == pytest.approx(0.000898, abs=1e-5)
    assert config.START_YEAR <= config.END_YEAR
    assert config.years()[0] == config.START_YEAR


def test_staged_uri_local_override(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STAGED_ROOT", str(tmp_path))
    assert config.staged_uri("forestLoss", "x.tif") == f"{tmp_path}/forestLoss/x.tif"


def test_list_units_shapes():
    for name, module in MODULES.items():
        units = module.list_units()
        assert units, name
        assert all("id" in u for u in units), name


def test_translate_to_cog(local_staged, tmp_path):
    # Write a tiny source raster, then re-COG it; a valid COG with the source's footprint.
    res, n = config.SCALE_DEG, 64
    transform = Affine.translation(-85.0, 9.0 + n * res) * Affine.scale(res, -res)
    src = str(tmp_path / "src.tif")
    with rio.open(src, "w", driver="GTiff", height=n, width=n, count=1,
                  dtype="uint8", crs="EPSG:4326", transform=transform) as ds:
        ds.write(np.ones((n, n), "uint8"), 1)

    dst = config.staged_uri("test", "t.tif")
    assert not s3io.exists(dst)
    cog.translate_to_cog(src, dst, resampling="nearest")
    assert s3io.exists(dst)

    valid, errors, _ = cog_validate(dst)
    assert valid, errors
    with rio.open(dst) as out:
        assert out.crs.to_epsg() == 4326
        assert out.dtypes[0] == "uint8"
    fp = cog.footprint(dst, tile_index.INDEX_CRS)
    assert fp[0] == pytest.approx(-85.0, abs=1e-6)
    assert fp[3] == pytest.approx(9.0 + n * res, abs=1e-6)

    # Always overwrites (existence checks are the orchestrator's job): a second call just rewrites it.
    cog.translate_to_cog(src, dst, resampling="nearest")
    assert s3io.exists(dst)


def test_index_build_and_lookup(local_staged):
    footprints = [
        ("s3://b/a.tif", (-90, 0, -80, 10)),
        ("s3://b/b.tif", (-80, 0, -70, 10)),
        ("s3://b/c.tif", (0, 0, 10, 10)),
    ]
    tile_index.build_index("forestLoss", footprints)

    # A bbox straddling the first two tiles returns both, not the third.
    hits = tile_index.lookup("forestLoss", (-81, 1, -79, 2))
    assert set(hits) == {"s3://b/a.tif", "s3://b/b.tif"}

    far = tile_index.lookup("forestLoss", (50, 50, 51, 51))
    assert far == []


def _write_cog(uri, bounds, n=8):
    """Write a tiny EPSG:4326 raster covering ``bounds`` (w, s, e, n) to ``uri``."""
    transform = rio.transform.from_bounds(*bounds, n, n)
    with s3io.staged_local_path(uri) as path, rio.open(
        path, "w", driver="GTiff", height=n, width=n, count=1,
        dtype="uint8", crs="EPSG:4326", transform=transform) as ds:
        ds.write(np.ones((n, n), "uint8"), 1)


def test_index_cogs_rebuilds_from_staged_cogs(local_staged):
    alb = config.staged_uri("population", "2020", "ALB_2020.tif")
    _write_cog(alb, (19, 39, 21, 43))
    _write_cog(config.staged_uri("population", "2020", "CRI_2020.tif"), (-86, 8, -82, 11))
    _write_cog(config.staged_uri("population", "2019", "ALB_2019.tif"), (19, 39, 21, 43))  # other year

    uri = tile_index.index_cogs("population", year=2020)
    assert s3io.exists(uri)

    # The 2019 COG is filtered out; a point in Albania hits only the 2020 tile.
    hits = tile_index.lookup("population", (20, 40, 20.5, 40.5), year=2020)
    assert hits == [alb]


def test_lookup_missing_index_returns_empty(local_staged):
    assert tile_index.lookup("forestLoss", (0, 0, 1, 1)) == []


# --------------------------------------------------------------------------------------
# Vector rasterization (gdal_rasterize-backed helper used by sdpt + roads)
# --------------------------------------------------------------------------------------
def test_snap_grid_snaps_to_bii_grid():
    res = config.SCALE_DEG
    w, h, snapped = cog.snap_grid((10.3, 50.1, 10.7, 50.6))
    # Origin snaps outward to a grid multiple; width/height match the snapped extent.
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
    # Footprint snaps outward, so it covers the requested bounds.
    fp = cog.footprint(dst, tile_index.INDEX_CRS)
    assert fp[0] <= bounds[0] and fp[3] >= bounds[3]

    # A thin diagonal line still burns (all_touched=True) -> no dropout.
    line = {"type": "LineString", "coordinates": [[10.0, 50.0], [10.5, 50.5]]}
    src2 = _write_geojson(tmp_path / "line.geojson", [line])
    dst2 = config.staged_uri("test", "line.tif")
    cog.rasterize_to_cog(src2, dst2, bounds)
    assert _open_cog_band(dst2).sum() > 0


# --------------------------------------------------------------------------------------
# sdpt / roads enumeration (no network)
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
    # ~12% of SDPT country layers are EPSG:3857/UTM. stage_unit reprojects the layer to the
    # EPSG:4326 BII grid (via sdpt._localized) before burning — gdal_rasterize burns coordinates
    # onto the degree grid as-is. Here the remote GDB is swapped for a local 3857 gpkg; without the
    # reproject the meter coordinates would miss the grid entirely (empty burn). ~0.3° box at 10°E.
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
    assert arr.sum() > 0  # the reprojected polygon actually burned onto the degree grid


@_needs_gdal_clis
def test_sdpt_stage_unit_reads_bounds_from_source(local_staged, tmp_path, monkeypatch):
    # With no explicit window, the per-country COG extent is the layer's own bounds, read back
    # from the localized copy (production stages whole countries; units carry no bounds).
    import geopandas as gpd
    from shapely.geometry import box

    from bii.staging import sdpt

    poly = box(10.0, 45.0, 10.3, 45.3)  # already EPSG:4326
    src = str(tmp_path / "wgs84.gpkg")
    gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326").to_file(src, driver="GPKG", layer="plant")
    monkeypatch.setattr(sdpt, "_source_path", lambda: src)

    assert sdpt.stage_unit({"id": "x", "region": "x", "layer": "plant"}) is True  # no bounds
    dst = sdpt._dst("x")
    # The footprint snaps outward from the layer's own extent — it encloses the polygon.
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
    # A known leaf region is present and filterable.
    cx = roads.list_units(regions=["christmas-island"])
    assert len(cx) == 1 and cx[0]["id"] == "christmas-island"
    # Duplicate-extent macro-regions are excluded; their children provide coverage instead.
    ids = {u["id"] for u in units}
    assert roads.GEOFABRIK_DROP_IDS.isdisjoint(ids)
    assert "us/delaware" in ids and "us" not in ids


def _open_cog_band(uri, band=1):
    with rio.open(uri) as src:
        return src.read(band)
