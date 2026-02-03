#!/bin/bash
# generate-eks-oidc-provider-lambda-layer.sh

# Build the Docker image
docker build $DOCKER_NETWORK -t eks-oidc-provider-layer-builder .

# Run the container and copy the zip file
docker run --rm \
  -v $(pwd)/../artifacts:/layer/artifacts \
  eks-oidc-provider-layer-builder \
  bash -c "chmod +x build-layer.sh && ./build-layer.sh && cp eks-oidc-provider-lambda-layer.zip /layer/artifacts/"

echo "Lambda layer zip file has been created in the artifacts directory"
