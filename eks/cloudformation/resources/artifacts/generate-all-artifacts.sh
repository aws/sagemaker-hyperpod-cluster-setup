#!/bin/bash
# generate-all-artifacts.sh

cd ../helm-chart-injector && ./generate-helm-lambda-zip.sh
cd ../inference-helm-chart-injector && ./generate-inf-helm-lambda-zip.sh
cd ../inference-k8s-service-account-creator && ./generate-inf-sa-creation-lambda-zip.sh
cd ../fsx-for-lustre && ./generate-fsx-lambda-zip.sh
cd ../hyperpod-cluster-creator && ./generate-hp-lambda-zip.sh
cd ../private-subnet-tagging && ./generate-lambda-zip.sh