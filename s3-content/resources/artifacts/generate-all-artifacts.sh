#!/bin/bash
# generate-all-artifacts.sh

cd ../helm-chart-injector && ./generate-helm-lambda-zip.sh
cd ../hyperpod-cluster-creator && ./generate-hp-lambda-zip.sh