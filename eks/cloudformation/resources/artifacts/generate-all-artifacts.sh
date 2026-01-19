#!/bin/bash
# generate-all-artifacts.sh

cd ../helm-chart-injector && ./generate-helm-lambda-zip.sh
cd ../inference-helm-chart-injector && ./generate-inf-helm-lambda-zip.sh
cd ../inference-k8s-service-account-creator && ./generate-inf-sa-creation-lambda-zip.sh
cd ../data-scientist-setup && ./generate-ds-setup-lambda-zip.sh
cd ../tiered-cache-config && ./generate-tiered-cache-lambda-zip.sh
cd ../cert-manager-installer && ./generate-cert-manager-lambda-zip.sh
cd ../common-eks-addon-installer && ./generate-common-eks-addon-lambda-zip.sh
cd ../hpto-addon-installer && ./generate-hpto-addon-lambda-zip.sh
cd ../fsx-for-lustre && ./generate-fsx-lambda-zip.sh
cd ../hyperpod-cluster-creator && ./generate-hp-lambda-zip.sh
cd ../private-subnet-tagging && ./generate-lambda-zip.sh
cd ../grafana-lambda-function && ./generate-lambda-zip.sh
cd ../observability-grafana-creator && ./generate-observability-grafana-creator-lambda-zip.sh
cd ../grafana-service-token && ./generate-grafana-service-token-lambda-zip.sh
cd ../observability-stack && ./generate-observability-stack-lambda-zip.sh
cd ../observability-stack && ./generate-observability-stack-lambda-zip.sh
cd ../cluster-policy && ./generate-cluster-policy-lambda-zip.sh
