#!/bin/bash
# generate-inference-operator-lambda-func.sh

# Create and activate a temporary virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Install dependencies from requirements.txt if it exists
if [ -f lambda_function/requirements.txt ]; then
    pip install -r lambda_function/requirements.txt -t package/
else
    mkdir -p package/
fi

# Copy function code to package directory
cp lambda_function/lambda_function.py package/

# Copy shared utilities
cp ../shared/version_utils.py package/

# Create ZIP file
cd package
zip -r ../../../resources3/artifacts/inference-operator-lambda-function.zip .
cd ..

# Clean up
rm -rf package venv
