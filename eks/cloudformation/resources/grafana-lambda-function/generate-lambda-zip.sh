#!/bin/bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LAMBDA_DIR="${SCRIPT_DIR}/lambda_function"
BUILD_DIR="${SCRIPT_DIR}/build"
ARTIFACTS_DIR="${SCRIPT_DIR}/../../resources/artifacts"

# Validate required files and directories
if [ ! -f "${LAMBDA_DIR}/lambda_function.py" ]; then
    echo "Error: lambda_function.py not found in ${LAMBDA_DIR}"
    exit 1
fi

if [ ! -f "${LAMBDA_DIR}/requirements.txt" ]; then
    echo "Error: requirements.txt not found in ${LAMBDA_DIR}"
    exit 1
fi

# Create build directories
echo "Preparing build directory..."
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

# Copy Lambda function files
echo "Copying Lambda function files..."
cp "${LAMBDA_DIR}/lambda_function.py" "${BUILD_DIR}/"
cp "${LAMBDA_DIR}/requirements.txt" "${BUILD_DIR}/"

# Install dependencies
echo "Installing dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r "${BUILD_DIR}/requirements.txt" -t "${BUILD_DIR}"

# Create zip
echo "Creating zip file..."
cd "${BUILD_DIR}"
zip -r ../grafana-lambda-function.zip .

# Move to artifacts
echo "Moving to artifacts directory..."
mkdir -p "${ARTIFACTS_DIR}"
mv ../grafana-lambda-function.zip "${ARTIFACTS_DIR}/"

# Cleanup
echo "Cleaning up..."
cd ..
rm -rf "${BUILD_DIR}"

echo "Build complete. Artifact created at: ${ARTIFACTS_DIR}/grafana-lambda-function.zip"