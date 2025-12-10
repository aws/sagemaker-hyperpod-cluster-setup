#!/bin/bash
# generate-cert-manager-lambda-func.sh

# Create and activate a temporary virtual environment
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Install dependencies from requirements.txt
pip install -r lambda_function/requirements.txt -t package/

# Copy function code to package directory
cp lambda_function/lambda_function.py package/

# Create ZIP file
cd package
zip -r ../../../resources3/artifacts/cert-manager-lambda-function.zip .
cd ..

# Clean up
rm -rf package venv
