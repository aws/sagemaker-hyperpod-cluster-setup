#!/bin/bash
# The function uses only the standard library and the boto3/botocore that come with the Lambda runtime.
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LAMBDA_DIR="${SCRIPT_DIR}/lambda_function"
ZIP="${SCRIPT_DIR}/../../resources/artifacts/slurm-observability-launcher-lambda-function.zip"

rm -f "${ZIP}"
zip -j "${ZIP}" "${LAMBDA_DIR}/lambda_function.py"

echo "Build complete. Artifact created at: ${ZIP}"
