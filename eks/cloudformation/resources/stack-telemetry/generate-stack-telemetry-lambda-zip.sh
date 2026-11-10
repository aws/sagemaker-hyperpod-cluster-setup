#!/bin/bash
# generate-stack-telemetry-lambda-zip.sh
# Packages the stack telemetry custom-resource Lambda. No third-party dependencies (boto3 is in the runtime).
set -euo pipefail
cd "$(dirname "$0")"
rm -f ../artifacts/stack-telemetry-lambda-function.zip
(cd lambda_function && zip -q ../../artifacts/stack-telemetry-lambda-function.zip lambda_function.py)
