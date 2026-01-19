#!/bin/bash
# generate-common-eks-addon-lambda-layer.sh

# Build the Docker image
docker build $DOCKER_NETWORK -t lambda-layer-builder-common-eks-addon .

# Run the container and copy the zip file
docker run --rm \
  -v $(pwd)/../../resources2/artifacts:/layer/artifacts \
  lambda-layer-builder-common-eks-addon \
  bash -c "chmod +x build-layer.sh && ./build-layer.sh && cp common-eks-addon-lambda-layer.zip /layer/artifacts/"

echo "Lambda layer zip file has been created in the artifacts directory"
