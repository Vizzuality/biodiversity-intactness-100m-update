#!/usr/bin/env bash
# Build and push the bii image to ECR; print the digest to pin into Batch job defs so local docker
# and Batch run the identical artifact.
#
#   ./scripts/push_images.sh            # tag = git short sha
#   ./scripts/push_images.sh v2         # explicit tag
#
# Needs: docker, awscli, AWS_REGION (+ creds) in the environment.
set -euo pipefail

: "${AWS_REGION:?set AWS_REGION (see .env)}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
TAG="${1:-$(git rev-parse --short HEAD)}"
IMAGE="$REGISTRY/bii:$TAG"

cd "$(dirname "$0")/.."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"

# Batch runs arm64 (Graviton); build arm64 to match.
docker build --platform linux/arm64 -t "$IMAGE" .
docker push "$IMAGE"

echo "--- pin this in infra (tofu apply -var image=...) ---"
docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE"
