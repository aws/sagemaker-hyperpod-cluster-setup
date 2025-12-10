#!/bin/bash

# Build script for cert-manager Lambda layer
# This layer includes kubectl and AWS IAM authenticator for EKS operations

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAYER_DIR="${SCRIPT_DIR}/cert-manager-lambda-layer"
PYTHON_DIR="${LAYER_DIR}/python"

# Set versions
KUBECTL_VERSION="v1.31.2"
AUTH_VERSION="0.6.11"

echo "Building cert-manager Lambda layer..."

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

# Create the layer zip
cd "${LAYER_DIR}"
echo "Creating layer zip file..."
zip -r "${SCRIPT_DIR}/cert-manager-lambda-layer.zip" .

echo "Layer build complete: ${SCRIPT_DIR}/cert-manager-lambda-layer.zip"
