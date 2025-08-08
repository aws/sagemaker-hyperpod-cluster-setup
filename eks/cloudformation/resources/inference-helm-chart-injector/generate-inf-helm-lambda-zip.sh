#!/bin/bash
# generate-inf-helm-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-inf-helm-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-inf-helm-lambda-func.sh

