#!/bin/bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Loading observability assets..."

# Write to current package directories
echo "Writing assets to current package..."
cd "$SCRIPT_DIR"
node -e "
const fs = require('fs');
const {
  clusterJson, efaJson, inferenceJson, tasksJson, trainingJson, rulesYaml,
  slurmAcceleratedMetricsJson, slurmEfaJson, slurmNodeJson, slurmNcclJson, slurmClusterJson,
  slurmJobJson
} = require('@amzn/hyperpod-observability-assets');

// Create directories
fs.mkdirSync('dashboards/templates', { recursive: true });
fs.mkdirSync('dashboards/slurmTemplates', { recursive: true });
fs.mkdirSync('alerts', { recursive: true });

// Write EKS dashboard files
fs.writeFileSync('dashboards/templates/cluster.json', JSON.stringify(clusterJson, null, 2));
fs.writeFileSync('dashboards/templates/efa.json', JSON.stringify(efaJson, null, 2));
fs.writeFileSync('dashboards/templates/inference.json', JSON.stringify(inferenceJson, null, 2));
fs.writeFileSync('dashboards/templates/tasks.json', JSON.stringify(tasksJson, null, 2));
fs.writeFileSync('dashboards/templates/training.json', JSON.stringify(trainingJson, null, 2));

// Write Slurm dashboard files
fs.writeFileSync('dashboards/slurmTemplates/acceleratedMetrics.json', JSON.stringify(slurmAcceleratedMetricsJson, null, 2));
fs.writeFileSync('dashboards/slurmTemplates/efa.json', JSON.stringify(slurmEfaJson, null, 2));
fs.writeFileSync('dashboards/slurmTemplates/node.json', JSON.stringify(slurmNodeJson, null, 2));
fs.writeFileSync('dashboards/slurmTemplates/nccl.json', JSON.stringify(slurmNcclJson, null, 2));
fs.writeFileSync('dashboards/slurmTemplates/cluster.json', JSON.stringify(slurmClusterJson, null, 2));
fs.writeFileSync('dashboards/slurmTemplates/job.json', JSON.stringify(slurmJobJson, null, 2));

// Write rules file
fs.writeFileSync('alerts/alert-rules.yaml', rulesYaml);

console.log('Assets written to current package');
"

echo "Observability assets created in current package"

# Create zip file excluding the script
echo "Creating zip file..."
cd "$SCRIPT_DIR"
mkdir -p "../../resources4/artifacts"
zip -r "../../resources4/artifacts/observability-assets.zip" dashboards/ alerts/
echo "Zip file created at: ../../resources4/artifacts/observability-assets.zip"