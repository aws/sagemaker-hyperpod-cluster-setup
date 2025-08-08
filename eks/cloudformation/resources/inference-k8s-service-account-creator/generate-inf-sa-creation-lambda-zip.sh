#!/bin/bash
# generate-inf-sa-creation-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-inf-sa-creation-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-inf-sa-creation-lambda-func.sh

