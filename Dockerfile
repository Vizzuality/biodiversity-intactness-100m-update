# Raster staging + processing image: GDAL/OGR CLIs (gdal_rasterize, ogr2ogr) from the base, the
# bii package + Python geo stack from wheels. One image for processing, the local chunk test, and
# all raster staging (Hansen, WorldPop, VNL, travel time, FML, SDPT). Build/tag as `bii`:
#   docker build -t bii .
# Pinned: ubuntu-small-latest floats to Python 3.14, which lacks wheels for several geo deps
# (color-operations, ...). 3.11.5 ships Python 3.12 where all wheels exist.
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.11.5
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv pip install --system --break-system-packages --no-cache '.[process]'

# Default to the processing worker; staging overrides the command to `bii-stage-worker`.
CMD ["bii-process"]
