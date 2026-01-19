#!/bin/bash

# Build script for common-eks-addon Lambda layer
# This layer includes boto3 for EKS API operations

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAYER_DIR="${SCRIPT_DIR}/common-eks-addon-lambda-layer"
PYTHON_DIR="${LAYER_DIR}/python"

echo "Building common-eks-addon Lambda layer..."

# Clean up previous builds
rm -rf "${LAYER_DIR}"
mkdir -p "${PYTHON_DIR}"

# Install Python dependencies
pip3 install boto3>=1.26.0 botocore>=1.29.0 -t "${PYTHON_DIR}"

# Create the layer zip
cd "${LAYER_DIR}"
echo "Creating layer zip file..."
zip -r "${SCRIPT_DIR}/common-eks-addon-lambda-layer.zip" .

echo "Layer build complete: ${SCRIPT_DIR}/common-eks-addon-lambda-layer.zip"
