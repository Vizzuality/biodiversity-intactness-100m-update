# Pinned: ubuntu-small-latest floats to Python 3.14, which lacks wheels for several geo deps;
# this tag ships Python 3.12 where all wheels exist.
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.11.5
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends osmctools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv pip install --system --break-system-packages --no-cache .

CMD ["bii-process"]
