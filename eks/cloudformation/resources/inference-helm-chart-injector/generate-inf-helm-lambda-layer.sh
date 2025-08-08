#!/bin/bash
# generate-inf-helm-lambda-layer.sh

# Build the Docker image
docker build $DOCKER_NETWORK -t lambda-layer-builder .

# Run the container and copy the zip file
docker run --rm \
  -v $(pwd)/../../resources2/artifacts:/layer/artifacts \
  lambda-layer-builder \
  bash -c "chmod +x build-layer.sh && ./build-layer.sh && cp inf-helm-lambda-layer.zip /layer/artifacts/"

echo "Lambda layer zip file has been created in the artifacts directory"

