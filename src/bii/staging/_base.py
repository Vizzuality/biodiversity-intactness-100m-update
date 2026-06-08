"""Shared conventions for staging modules.

Every staging module exposes the same pair so the CLI / orchestrator can treat them
uniformly and fan out one Batch array job per unit:

    list_units(...) -> list[dict]      # enumerate work; each unit has a stable "id"
    stage_unit(unit, ...) -> dict|None # stage one unit; None == skipped (e.g. ocean tile)

A staged result is ``{asset, uri, footprint, year, index_part}``.
"""

from __future__ import annotations

from .. import tile_index


def finalize(asset: str, dst: str, footprint, year: int | None, register_index: bool) -> dict:
    """Register the staged COG's footprint (as an index part) and return the result dict."""
    part = tile_index.register(asset, dst, footprint, year) if register_index else None
    return {
        "asset": asset,
        "uri": dst,
        "footprint": list(footprint),
        "year": year,
        "index_part": part,
    }
