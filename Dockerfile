FROM ghcr.io/osgeo/gdal:ubuntu-small-3.11.5
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends osmctools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-cache
ENV PATH="/app/.venv/bin:$PATH"

CMD ["uv", "run", "bii-process"]
