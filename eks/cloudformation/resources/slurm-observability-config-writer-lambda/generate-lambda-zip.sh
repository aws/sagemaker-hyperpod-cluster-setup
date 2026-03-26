#!/bin/bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LAMBDA_DIR="${SCRIPT_DIR}/lambda_function"
BUILD_DIR="${SCRIPT_DIR}/build"
ARTIFACTS_DIR="${SCRIPT_DIR}/../../resources/artifacts"

if [ ! -f "${LAMBDA_DIR}/lambda_function.py" ]; then
    echo "Error: lambda_function.py not found in ${LAMBDA_DIR}"
    exit 1
fi

if [ ! -f "${LAMBDA_DIR}/requirements.txt" ]; then
    echo "Error: requirements.txt not found in ${LAMBDA_DIR}"
    exit 1
fi

echo "Preparing build directory..."
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

echo "Copying Lambda function files..."
cp "${LAMBDA_DIR}/lambda_function.py" "${BUILD_DIR}/"
cp "${LAMBDA_DIR}/requirements.txt" "${BUILD_DIR}/"

echo "Installing dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r "${BUILD_DIR}/requirements.txt" -t "${BUILD_DIR}"

echo "Creating zip file..."
cd "${BUILD_DIR}"
zip -r ../slurm-observability-config-writer-lambda-function.zip .

echo "Moving to artifacts directory..."
mkdir -p "${ARTIFACTS_DIR}"
mv ../slurm-observability-config-writer-lambda-function.zip "${ARTIFACTS_DIR}/"

echo "Cleaning up..."
cd ..
rm -rf "${BUILD_DIR}"

echo "Build complete. Artifact created at: ${ARTIFACTS_DIR}/slurm-observability-config-writer-lambda-function.zip"
