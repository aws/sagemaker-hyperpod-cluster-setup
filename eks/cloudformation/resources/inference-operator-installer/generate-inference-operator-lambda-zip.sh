#!/bin/bash
# generate-inference-operator-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-inference-operator-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-inference-operator-lambda-func.sh
