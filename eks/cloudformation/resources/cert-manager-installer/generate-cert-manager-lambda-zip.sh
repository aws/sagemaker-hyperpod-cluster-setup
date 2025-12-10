#!/bin/bash
# generate-cert-manager-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-cert-manager-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-cert-manager-lambda-func.sh
