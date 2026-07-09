#!/usr/bin/env bash
# Redeploy: build + push the current HEAD image and pin its digest into the Batch job defs.
set -euo pipefail

: "${AWS_REGION:?set AWS_REGION (see .env)}"
cd "$(dirname "$0")/.."

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
IMAGE="$REGISTRY/bii:$(git rev-parse --short HEAD)"

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/arm64 -t "$IMAGE" .  # Batch runs arm64 (Graviton); match it
docker push "$IMAGE"

DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE")
(cd infra && tofu apply -var "image=$DIGEST")
