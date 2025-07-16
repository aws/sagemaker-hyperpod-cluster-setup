#!/bin/bash
set -e  # Exit on any error
# set -x  # Print commands as they are executed

echo "Starting layer build..."

# Create directory structure for Python runtime
mkdir -p fsx-lambda-layer/python/bin
mkdir -p fsx-lambda-layer/python/lib

# Set versions
KUBECTL_VERSION="v1.31.2"
HELM_VERSION="v3.15.3"
AUTH_VERSION="0.6.11"
EKSCTL_VERSION="0.195.0"

# Download and install kubectl (compressed version)
curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
chmod +x kubectl
mv kubectl fsx-lambda-layer/python/bin/

# Download and install eksctl
curl -sLO "https://github.com/eksctl-io/eksctl/releases/download/v${EKSCTL_VERSION}/eksctl_Linux_amd64.tar.gz"
tar -xzf eksctl_Linux_amd64.tar.gz -C /tmp && rm eksctl_Linux_amd64.tar.gz
mv /tmp/eksctl fsx-lambda-layer/python/bin/
chmod +x fsx-lambda-layer/python/bin/eksctl

# Download and install aws-iam-authenticator 
echo "Downloading aws-iam-authenticator..."
curl -Lo fsx-lambda-layer/python/bin/aws-iam-authenticator \
    "https://github.com/kubernetes-sigs/aws-iam-authenticator/releases/download/v${AUTH_VERSION}/aws-iam-authenticator_${AUTH_VERSION}_linux_amd64" \
    --fail \
    --verbose 

# Verify the download
if [ ! -s fsx-lambda-layer/python/bin/aws-iam-authenticator ]; then
    echo "Error: aws-iam-authenticator download failed or file is empty"
    exit 1
fi

echo "Making aws-iam-authenticator executable..."
chmod +x fsx-lambda-layer/python/bin/aws-iam-authenticator

# Verify the file
echo "Checking aws-iam-authenticator..."
file fsx-lambda-layer/python/bin/aws-iam-authenticator
ls -l fsx-lambda-layer/python/bin/aws-iam-authenticator


# Copy shared libraries
echo "Copying shared libraries..."
echo "Finding and copying required libraries (excluding libc.so.6)..."
for binary in fsx-lambda-layer/python/bin/*; do
    if [ -f "$binary" ] && [ -x "$binary" ]; then
        echo "Analyzing dependencies for $binary..."
        ldd "$binary" 2>/dev/null | \
            grep "=> /" | \
            awk '{print $3}' | \
            grep -v 'libc.so.6' | \
            while read -r lib; do
                if [ -f "$lib" ]; then
                    echo "Copying $lib..."
                    cp -L "$lib" fsx-lambda-layer/python/lib/
                fi
            done
    fi
done

echo "Verifying layer contents..."
echo "=== Contents of python/bin ==="
ls -la fsx-lambda-layer/python/bin/
echo "=== Contents of python/lib ==="
ls -la fsx-lambda-layer/python/lib/

# Show component sizes before zipping
echo "=== Component sizes ==="
du -sh fsx-lambda-layer/python/bin/*
du -sh fsx-lambda-layer/python/lib

echo "Creating zip file..."
# Create the layer zip file with maximum compression
cd fsx-lambda-layer
zip -9 -r ../fsx-lambda-layer.zip .
cd ..

# Show final zip size
echo "=== Final zip size ==="
du -sh fsx-lambda-layer.zip

# Show uncompressed size for verification
echo "=== Uncompressed size ==="
unzip -l fsx-lambda-layer.zip | tail -1 | awk '{print $1}'

echo "Layer build complete!"
