#!/usr/bin/env bash
# Build the merged bii image and push it to ECR, then print the immutable digest to pin into the
# Batch job definitions (infra/ image) so local docker (`stage.py --executor docker`) and Batch run
# the identical artifact.
#
#   ./scripts/push_images.sh            # tag = git short sha
#   ./scripts/push_images.sh v2         # explicit tag
#
# Needs: docker, awscli, AWS_REGION (+ creds) in the environment. The ECR repo comes from `tofu apply`.
set -euo pipefail

: "${AWS_REGION:?set AWS_REGION (see .env)}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
TAG="${1:-$(git rev-parse --short HEAD)}"
IMAGE="$REGISTRY/bii:$TAG"

cd "$(dirname "$0")/.."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"

# --platform: Batch runs x86 (r6id/r5d/r5dn), so force amd64 even when building on an arm64 Mac.
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

echo "--- pin this in infra (tofu apply -var image=...) ---"
docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE"
