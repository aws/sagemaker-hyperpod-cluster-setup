#!/bin/bash
# generate-cert-manager-lambda-layer.sh

# Build the Docker image
docker build $DOCKER_NETWORK -t lambda-layer-builder-cert-manager .

# Run the container and copy the zip file
docker run --rm \
  -v $(pwd)/../../resources3/artifacts:/layer/artifacts \
  lambda-layer-builder-cert-manager \
  bash -c "chmod +x build-layer.sh && ./build-layer.sh && cp cert-manager-lambda-layer.zip /layer/artifacts/"

echo "Lambda layer zip file has been created in the artifacts directory"
