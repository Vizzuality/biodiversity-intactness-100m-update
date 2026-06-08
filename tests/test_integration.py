"""Integration tests: stage ONE unit of each kind locally, end to end, against real sources.

These hit the network (marked ``integration`` — deselect with ``-m "not integration"``).
They are the local single-tile gate: stage one tile/country, then confirm the output is a
valid COG and the tile index can find it.

* WorldPop — a *multipart* dataset; stage one whole (small) country.
* SDPT     — a per-country GDB layer (vector); rasterize one small window + verify empty skip.
* roads    — a Geofabrik region (vector); download a tiny extract, filter highways, rasterize.
"""

import shutil

import numpy as np
import pytest
import rasterio as rio
from rio_cogeo.cogeo import cog_validate

from bii import tile_index
from bii.staging import roads, sdpt, worldpop

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
    result = worldpop.stage_unit(unit)

    assert result is not None
    _assert_valid_cog(result["uri"])

    # Albania ~ (19.3, 39.6, 21.1, 42.7); a point inside it should hit the tile.
    tile_index.consolidate("population", year=2020)
    hits = tile_index.lookup("population", (20.0, 41.0, 20.1, 41.1), year=2020)
    assert result["uri"] in hits


def test_stage_sdpt_country_window_and_empty_skip(data_staged):
    unit = {"id": "cri", "region": "cri", "layer": "cri_plant_v21"}

    # A window with no plantation polygons -> empty read -> skipped (returns None, writes
    # nothing). Checked first since the per-region dst is shared across windows.
    assert sdpt.stage_unit(unit, bounds=(-88.0, 5.0, -87.5, 5.5)) is None

    # Costa Rica (the project's test region) — its GDB layer has plantation polygons.
    result = sdpt.stage_unit(unit, bounds=(-85.5, 9.5, -84.5, 10.5))
    assert result is not None
    arr = _assert_valid_cog(result["uri"], dtype="uint8")
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.max() == 1  # some planted forest present
    assert result["year"] is None  # single-epoch

    # SDPT shares the forestManagement asset with FML; the index finds the staged tile.
    tile_index.consolidate("forestManagement")
    w, s, e, n = result["footprint"]
    mid = ((w + e) / 2, (s + n) / 2)
    hits = tile_index.lookup("forestManagement", (mid[0], mid[1], mid[0] + 0.01, mid[1] + 0.01))
    assert result["uri"] in hits


@pytest.mark.skipif(
    not (shutil.which("osmconvert") and shutil.which("osmfilter")),
    reason="osmctools not installed (required for roads staging)",
)
def test_stage_roads_region(data_staged):
    _check_roads_region()


def _check_roads_region():
    # Christmas Island — a tiny Geofabrik extract, fast to download + filter.
    unit = next(u for u in roads.list_units(regions=["christmas-island"]))
    result = roads.stage_unit(unit)

    assert result is not None
    arr = _assert_valid_cog(result["uri"], dtype="uint8")
    assert set(np.unique(arr)).issubset({0, 1})
    assert arr.max() == 1  # highways burned
    assert result["year"] is None  # single-epoch

    tile_index.consolidate("roads")
    w, s, e, n = result["footprint"]
    mid = ((w + e) / 2, (s + n) / 2)
    hits = tile_index.lookup("roads", (mid[0], mid[1], mid[0] + 0.01, mid[1] + 0.01))
    assert result["uri"] in hits
