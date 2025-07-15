#!/bin/bash
# generate-helm-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-helm-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-helm-lambda-func.sh

