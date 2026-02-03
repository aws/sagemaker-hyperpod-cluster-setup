#!/bin/bash
# generate-eks-oidc-provider-lambda-zip.sh
# This script generates both the lambda layer and function zip files

set -e

echo "Generating EKS OIDC Provider Lambda artifacts..."

# Generate lambda layer
echo "Step 1: Building lambda layer..."
./generate-eks-oidc-provider-lambda-layer.sh

# Generate lambda function
echo "Step 2: Building lambda function..."
./generate-eks-oidc-provider-lambda-func.sh

echo "All EKS OIDC Provider Lambda artifacts generated successfully!"
echo "Artifacts location: ../artifacts/"
echo "  - eks-oidc-provider-lambda-layer.zip"
echo "  - eks-oidc-provider-lambda-function.zip"
