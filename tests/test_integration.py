"""Integration tests: stage ONE unit of each kind locally, end to end, against real sources.

Network-bound (marked ``integration`` — deselect with ``-m "not integration"``).

* WorldPop — multipart dataset; stage one whole small country.
* SDPT     — per-country GDB layer (vector); rasterize.
* roads    — Geofabrik region (vector); download extract, filter highways, rasterize.
* iolulc   — landcover; index-only, walk the IO STAC for one year.
"""

import shutil

import numpy as np
import pytest
import rasterio as rio
from rio_cogeo.cogeo import cog_validate

from bii import tile_index
from bii import cog
from bii.staging import iolulc, roads, sdpt, worldpop

pytestmark = pytest.mark.integration


def _assert_valid_cog(uri, dtype=None):
    valid, errors, _ = cog_validate(uri)
    assert valid, errors
    with rio.open(uri) as src:
        assert src.crs.to_epsg() == 4326
        if dtype:
            assert src.dtypes[0] == dtype
        return src.read(1)


def test_stage_worldpop_country(data_staged):
    unit = {"id": "ALB_2020", "iso3": "ALB", "year": 2020,
            "url": worldpop._url("ALB", 2020)}
    assert worldpop.stage_unit(unit) is True
    dst = worldpop._dst("ALB", 2020)
    _assert_valid_cog(dst)

    # point inside Albania ~ (19.3, 39.6, 21.1, 42.7).
    tile_index.index_cogs("population", year=2020)
    hits = tile_index.lookup("population", (20.0, 41.0, 20.1, 41.1), year=2020)
    assert dst in hits


def test_stage_sdpt_country(data_staged):
    # El Salvador — small (~175 polygons) and EPSG:3857, so it exercises the reproject-to-grid
    # path (~12% of SDPT layers are 3857/UTM; gdal_rasterize won't reproject).
    unit = {"id": "slv", "region": "slv", "layer": "slv_plant_v21"}
    assert sdpt.stage_unit(unit) is True
    dst = sdpt._dst("slv")
    arr = _assert_valid_cog(dst, dtype="uint8")
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.max() == 1

    # SDPT shares the forestManagement asset with FML.
    tile_index.index_cogs("forestManagement")
    w, s, e, n = cog.footprint(dst, tile_index.INDEX_CRS)
    mid = ((w + e) / 2, (s + n) / 2)
    hits = tile_index.lookup("forestManagement", (mid[0], mid[1], mid[0] + 0.01, mid[1] + 0.01))
    assert dst in hits


def test_stage_iolulc_index(data_staged):
    # Index-only: no COG produced; index rows point at the original STAC hrefs, read in place.
    unit = {"id": "2020", "year": 2020}
    assert iolulc.stage_unit(unit) is True

    # Costa Rica test bounds.
    hits = tile_index.lookup("landcover", (-86, 9, -84, 11), year=2020)
    assert hits, "expected landcover index to cover the Costa Rica test bounds"
    assert all(isinstance(h, str) for h in hits)

    # Always rebuilds; existence checks are the orchestrator's job.
    assert iolulc.stage_unit(unit) is True


@pytest.mark.skipif(
    not (shutil.which("osmconvert") and shutil.which("osmfilter")),
    reason="osmctools not installed (required for roads staging)",
)
def test_stage_roads_region(data_staged):
    # Christmas Island — a tiny Geofabrik extract.
    unit = next(u for u in roads.list_units(regions=["christmas-island"]))
    assert roads.stage_unit(unit) is True
    dst = unit["dst"]
    arr = _assert_valid_cog(dst, dtype="uint8")
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.max() == 1

    tile_index.index_cogs("roads")
    w, s, e, n = cog.footprint(dst, tile_index.INDEX_CRS)
    mid = ((w + e) / 2, (s + n) / 2)
    hits = tile_index.lookup("roads", (mid[0], mid[1], mid[0] + 0.01, mid[1] + 0.01))
    assert dst in hits
