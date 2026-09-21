#!/bin/sh
set -e

IMAGE_NAME="${IMAGE_NAME:-f5-aigr-perf}"
TAG="${TAG:-latest}"

case "${1:-local}" in
  local)
    echo "Building for local platform only..."
    docker build -t "${IMAGE_NAME}:${TAG}" .
    ;;
  multi)
    echo "Building multi-platform image (amd64 + arm64)..."
    docker buildx build \
      --platform linux/amd64,linux/arm64 \
      -t "${IMAGE_NAME}:${TAG}" \
      --push \
      .
    ;;
  multi-load)
    echo "Building multi-platform image (amd64 + arm64) and loading locally..."
    docker buildx build \
      --platform linux/amd64,linux/arm64 \
      -t "${IMAGE_NAME}:${TAG}" \
      --load \
      .
    ;;
  *)
    echo "Usage: $0 [local|multi|multi-load]"
    echo ""
    echo "  local       Build for current platform only (default)"
    echo "  multi       Build amd64+arm64 and push to registry"
    echo "  multi-load  Build amd64+arm64 and load locally"
    echo ""
    echo "Environment variables:"
    echo "  IMAGE_NAME  Image name (default: f5-aigr-perf)"
    echo "  TAG         Image tag (default: latest)"
    exit 1
    ;;
esac

echo "Done: ${IMAGE_NAME}:${TAG}"
