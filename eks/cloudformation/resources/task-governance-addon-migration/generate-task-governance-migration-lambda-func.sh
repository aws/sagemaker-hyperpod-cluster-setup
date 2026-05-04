#!/bin/bash
set -euo pipefail
# generate-task-governance-migration-lambda-func.sh

# Anchor to this script's directory so relative paths resolve correctly
# regardless of the caller's working directory.
cd "$(dirname "$0")"

# Clean up any leftover artifacts from a previous failed run
rm -rf package venv

# Create and activate a temporary virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Install dependencies from requirements.txt
pip install -r lambda_function/requirements.txt -t package/

# Copy function code to package directory
cp lambda_function/lambda_function.py package/

# Create ZIP file (rm -f first to avoid merging into stale archive)
cd package
rm -f ../../artifacts/task-governance-addon-migration-lambda-function.zip
zip -r ../../artifacts/task-governance-addon-migration-lambda-function.zip .
cd ..

# Clean up
rm -rf package venv
