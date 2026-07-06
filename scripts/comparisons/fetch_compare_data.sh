#!/usr/bin/env bash
# Fetch external 2020 BII/human-modification datasets for comparison. Saves to data/compare/.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)/data/compare"
mkdir -p "$DIR"

# 2020 Global Human Modification (gHM v3, overall = all threats combined, 300 m; Theobald et al. 2025)
# https://zenodo.org/records/15191928  (9.5 GB)
curl -fL -C - -o "$DIR/ghm_2020_AA_300m.tif" \
  "https://zenodo.org/api/records/15191928/files/HMv20240801_2020c_AA.tif/content"

# Expert-elicited BII for sub-Saharan Africa (Clements et al. 2025; 1 km, afrotropics)
# https://doi.org/10.6084/m9.figshare.29773169  (585 MB)
curl -fL -C - -o "$DIR/bii_africa_1km.tif" \
  "https://ndownloader.figshare.com/files/58480429"

# NHM BII v2.1.1 (De Palma et al. 2024; global rasters 2000-2020 @ ~10 km, includes 2020; CC-BY-NC-SA)
# Portal is behind a Cloudflare JS challenge, so curl can't fetch it -- download in a browser:
#   https://data.nhm.ac.uk/dataset/bii-developed-by-nhm-v2-1-1-limited-release
# and save the zip as: $DIR/nhm_bii_v2.1.1.zip
