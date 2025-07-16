#!/bin/bash
# generate-fsx-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-fsx-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-fsx-lambda-func.sh

