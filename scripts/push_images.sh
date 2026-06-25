#!/usr/bin/env bash
# Build the bii / bii-roads images and push them to ECR, then print the immutable digests to pin
# into the Batch job definitions (infra/ raster_image / roads_image) so local docker (`stage.py
# --executor docker`) and Batch run the identical artifact.
#
#   ./scripts/push_images.sh            # tag = git short sha
#   ./scripts/push_images.sh v2         # explicit tag
#
# Needs: docker, awscli, AWS_REGION (+ creds) in the environment. ECR repos come from `tofu apply`.
set -euo pipefail

: "${AWS_REGION:?set AWS_REGION (see .env)}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
TAG="${1:-$(git rev-parse --short HEAD)}"
RASTER="$REGISTRY/bii:$TAG"
ROADS="$REGISTRY/bii-roads:$TAG"

cd "$(dirname "$0")/.."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"

docker build -t "$RASTER" .
docker build -t "$ROADS" --build-arg "BASE=$RASTER" -f Dockerfile.roads .
docker push "$RASTER"
docker push "$ROADS"

echo "--- pin these in infra (tofu apply -var raster_image=... -var roads_image=...) ---"
docker inspect --format '{{index .RepoDigests 0}}' "$RASTER"
docker inspect --format '{{index .RepoDigests 0}}' "$ROADS"
