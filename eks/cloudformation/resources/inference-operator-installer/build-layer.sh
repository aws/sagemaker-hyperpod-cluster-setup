#!/bin/bash

# Build script for Inference Operator Lambda layer
# This layer includes kubectl and dependencies for inference operations

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAYER_DIR="${SCRIPT_DIR}/inference-operator-lambda-layer"
PYTHON_DIR="${LAYER_DIR}/python"

# Set versions
KUBECTL_VERSION="v1.31.2"
AUTH_VERSION="0.6.11"

echo "Building Inference Operator Lambda layer..."

# Clean up previous builds
rm -rf "${LAYER_DIR}"
mkdir -p "${PYTHON_DIR}"

# Create bin directory
mkdir -p "${PYTHON_DIR}/bin"

# Create a temporary directory for downloads
TEMP_DIR=$(mktemp -d)
trap "rm -rf ${TEMP_DIR}" EXIT

cd "${TEMP_DIR}"

# Download kubectl
echo "Downloading kubectl..."
curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
chmod +x kubectl
mv kubectl "${PYTHON_DIR}/bin/"

# Download and install aws-iam-authenticator 
echo "Downloading aws-iam-authenticator..."
curl -Lo ${PYTHON_DIR}/bin/aws-iam-authenticator \
    "https://github.com/kubernetes-sigs/aws-iam-authenticator/releases/download/v${AUTH_VERSION}/aws-iam-authenticator_${AUTH_VERSION}_linux_amd64" \
    --fail \
    --verbose 
chmod +x ${PYTHON_DIR}/bin/aws-iam-authenticator

# Install Python dependencies if requirements.txt exists
if [ -f "${SCRIPT_DIR}/lambda_function/requirements.txt" ]; then
    echo "Installing Python dependencies..."
    pip3 install -r "${SCRIPT_DIR}/lambda_function/requirements.txt" -t "${PYTHON_DIR}/"
fi

# Copy shared utilities if they exist
if [ -f "${SCRIPT_DIR}/shared/version_utils.py" ]; then
    echo "Copying shared utilities..."
    cp "${SCRIPT_DIR}/shared/version_utils.py" "${PYTHON_DIR}/"
fi

# Create the layer zip
cd "${LAYER_DIR}"
echo "Creating layer zip file..."
zip -r "${SCRIPT_DIR}/inference-operator-lambda-layer.zip" .

echo "Layer build complete: ${SCRIPT_DIR}/inference-operator-lambda-layer.zip"
