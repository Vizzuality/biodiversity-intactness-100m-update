"""Unit tests for staging — no network. Exercise config, the COG writer, and the index."""

import shutil

import numpy as np
import pytest
import rasterio as rio
from affine import Affine
from rio_cogeo.cogeo import cog_validate

from bii import config, tile_index
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


def test_translate_to_cog_and_skip(local_staged, tmp_path):
    # Write a tiny source raster, then re-COG it; a valid COG with the source's footprint.
    res, n = config.SCALE_DEG, 64
    transform = Affine.translation(-85.0, 9.0 + n * res) * Affine.scale(res, -res)
    src = str(tmp_path / "src.tif")
    with rio.open(src, "w", driver="GTiff", height=n, width=n, count=1,
                  dtype="uint8", crs="EPSG:4326", transform=transform) as ds:
        ds.write(np.ones((n, n), "uint8"), 1)

    dst = config.staged_uri("test", "t.tif")
    assert not cog.exists(dst)
    fp = cog.translate_to_cog(src, dst, resampling="nearest")
    assert cog.exists(dst)

    valid, errors, _ = cog_validate(dst)
    assert valid, errors
    with rio.open(dst) as out:
        assert out.crs.to_epsg() == 4326
        assert out.dtypes[0] == "uint8"
    assert fp[0] == pytest.approx(-85.0, abs=1e-6)
    assert fp[3] == pytest.approx(9.0 + n * res, abs=1e-6)

    # Skip-if-exists: a second call returns the same footprint without rewriting.
    assert cog.translate_to_cog(src, dst, resampling="nearest") == fp


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


def test_index_register_and_consolidate(local_staged):
    tile_index.register("population", "s3://b/ALB_2020.tif", (19, 39, 21, 43), year=2020)
    tile_index.register("population", "s3://b/CRI_2020.tif", (-86, 8, -82, 11), year=2020)
    uri = tile_index.consolidate("population", year=2020)
    assert cog.exists(uri)

    hits = tile_index.lookup("population", (20, 40, 20.5, 40.5), year=2020)
    assert hits == ["s3://b/ALB_2020.tif"]


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
    fp = cog.rasterize_to_cog(src, dst, bounds)

    valid, errors, _ = cog_validate(dst)
    assert valid, errors
    arr = _open_cog_band(dst)
    assert arr.dtype == np.uint8
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.sum() > 0
    # Footprint snaps outward, so it covers the requested bounds.
    assert fp[0] <= bounds[0] and fp[3] >= bounds[3]

    # A thin diagonal line still burns (all_touched=True) -> no dropout.
    line = {"type": "LineString", "coordinates": [[10.0, 50.0], [10.5, 50.5]]}
    src2 = _write_geojson(tmp_path / "line.geojson", [line])
    dst2 = config.staged_uri("test", "line.tif")
    cog.rasterize_to_cog(src2, dst2, bounds)
    assert _open_cog_band(dst2).sum() > 0


@_needs_gdal_rasterize
def test_rasterize_to_cog_empty_is_skipped(local_staged, tmp_path):
    src = _write_geojson(tmp_path / "empty.geojson", [])
    dst = config.staged_uri("test", "empty.tif")
    assert cog.rasterize_to_cog(src, dst, (0.0, 0.0, 0.2, 0.2)) is None
    assert not cog.exists(dst)


def test_rasterize_to_cog_window_without_features_is_skipped(local_staged, tmp_path):
    # The layer has a feature, but none falls in the requested window -> skipped before burning
    # (no gdal_rasterize needed: the pre-check returns None).
    poly = {"type": "Polygon",
            "coordinates": [[[10.1, 50.1], [10.4, 50.1], [10.4, 50.4], [10.1, 50.4], [10.1, 50.1]]]}
    src = _write_geojson(tmp_path / "elsewhere.geojson", [poly])
    dst = config.staged_uri("test", "elsewhere.tif")
    assert cog.rasterize_to_cog(src, dst, (0.0, 0.0, 0.2, 0.2)) is None
    assert not cog.exists(dst)


@_needs_gdal_clis
def test_rasterize_to_cog_reprojects_non_4326_source(local_staged, tmp_path):
    # A source in EPSG:3857 (like ~12% of the SDPT country layers) must be reprojected before
    # burning — gdal_rasterize burns coordinates onto the degree grid as-is. The helper stages it
    # to a local 4326 copy via ogr2ogr first; without that the meter coordinates miss the grid
    # entirely (empty burn) or blow up snap_grid. Polygon ~ a 0.3° box at 10°E, 45°N.
    import geopandas as gpd
    from shapely.geometry import box

    # 10°E,45°N -> EPSG:3857 ~ (1113194, 5621521); +0.3° ~ +33000 / +47000 m.
    gdf = gpd.GeoDataFrame(
        geometry=[box(1113194, 5621521, 1146600, 5668500)], crs="EPSG:3857"
    )
    src = str(tmp_path / "mercator.gpkg")
    gdf.to_file(src, driver="GPKG", layer="plant")
    dst = config.staged_uri("test", "reproj.tif")

    bounds = (9.9, 44.9, 10.4, 45.4)  # the window, in EPSG:4326 (the BII grid CRS)
    fp = cog.rasterize_to_cog(src, dst, bounds, layer="plant")

    assert fp is not None
    valid, errors, _ = cog_validate(dst)
    assert valid, errors
    with rio.open(dst) as r:
        assert r.crs.to_epsg() == 4326
        arr = r.read(1)
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.sum() > 0  # the reprojected polygon actually burned onto the degree grid


@_needs_gdal_rasterize
def test_rasterize_to_cog_bounds_from_source(local_staged, tmp_path):
    # bounds=None -> extent read from the layer's metadata (no geometry load), snapped outward.
    poly = {"type": "Polygon",
            "coordinates": [[[1.0, 1.0], [1.2, 1.0], [1.2, 1.2], [1.0, 1.2], [1.0, 1.0]]]}
    src = _write_geojson(tmp_path / "auto.geojson", [poly])
    dst = config.staged_uri("test", "auto.tif")
    fp = cog.rasterize_to_cog(src, dst, None)
    assert fp is not None
    # Snapped footprint encloses the polygon extent.
    assert fp[0] <= 1.0 and fp[1] <= 1.0 and fp[2] >= 1.2 and fp[3] >= 1.2
    assert _open_cog_band(dst).sum() > 0


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
