#!/bin/bash
# generate-common-eks-addon-lambda-zip.sh

# Build the Lambda layer using Docker
./generate-common-eks-addon-lambda-layer.sh

# Package the Lambda function with dependencies
./generate-common-eks-addon-lambda-func.sh
