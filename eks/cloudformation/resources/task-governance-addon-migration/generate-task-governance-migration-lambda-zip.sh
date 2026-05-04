#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# Package the Lambda function with dependencies
./generate-task-governance-migration-lambda-func.sh
