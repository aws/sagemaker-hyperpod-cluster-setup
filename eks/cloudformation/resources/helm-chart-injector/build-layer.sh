#!/bin/bash
set -e  # Exit on any error
# set -x  # Print commands as they are executed

echo "Starting layer build..."

# Create directory structure for Python runtime
mkdir -p helm-lambda-layer/python/bin
mkdir -p helm-lambda-layer/python/lib
mkdir -p helm-lambda-layer/python/libexec/git-core

# Set versions
KUBECTL_VERSION="v1.31.2"
HELM_VERSION="v3.15.3"
AUTH_VERSION="0.6.11"

# Download and install kubectl (compressed version)
curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
chmod +x kubectl
mv kubectl helm-lambda-layer/python/bin/

# Download and install Helm (using minimal tarball)
curl -LO "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz"
tar -zxvf helm-${HELM_VERSION}-linux-amd64.tar.gz --strip-components=1 linux-amd64/helm
mv helm helm-lambda-layer/python/bin/
rm -f helm-${HELM_VERSION}-linux-amd64.tar.gz

# Download and install aws-iam-authenticator 
echo "Downloading aws-iam-authenticator..."
curl -Lo helm-lambda-layer/python/bin/aws-iam-authenticator \
    "https://github.com/kubernetes-sigs/aws-iam-authenticator/releases/download/v${AUTH_VERSION}/aws-iam-authenticator_${AUTH_VERSION}_linux_amd64" \
    --fail \
    --verbose 

# Verify the download
if [ ! -s helm-lambda-layer/python/bin/aws-iam-authenticator ]; then
    echo "Error: aws-iam-authenticator download failed or file is empty"
    exit 1
fi

echo "Making aws-iam-authenticator executable..."
chmod +x helm-lambda-layer/python/bin/aws-iam-authenticator

# Verify the file
echo "Checking aws-iam-authenticator..."
file helm-lambda-layer/python/bin/aws-iam-authenticator
ls -l helm-lambda-layer/python/bin/aws-iam-authenticator

# Install yq and make yq executable...
echo "Install yq and make yq executable..."
wget https://github.com/mikefarah/yq/releases/download/v4.46.1/yq_linux_amd64 -O helm-lambda-layer/python/bin/yq
chmod +x helm-lambda-layer/python/bin/yq

# Install minimal git
echo "Installing git..."
yum install -y git-core expat
yum list installed | grep git

echo "Checking git binary location..."
which git
ls -l $(which git)

# Copy git binary and make it executable
echo "Copying git binary..."
cp /usr/bin/git helm-lambda-layer/python/bin/
chmod +x helm-lambda-layer/python/bin/git

# Copy essential git commands and make them executable
echo "Copying git components..."
essential_git_commands=(
    "git-remote-https"
    "git-clone"
)

for cmd in "${essential_git_commands[@]}"; do
    echo "Copying $cmd..."
    if [ -f "/usr/libexec/git-core/$cmd" ]; then
        cp "/usr/libexec/git-core/$cmd" helm-lambda-layer/python/libexec/git-core/
        chmod +x "helm-lambda-layer/python/libexec/git-core/$cmd"
    else
        echo "WARNING: $cmd not found in /usr/libexec/git-core/"
        # Check if it exists elsewhere
        find / -name "$cmd" 2>/dev/null || echo "Could not find $cmd anywhere"
    fi
done

# Copy shared libraries
echo "Copying shared libraries..."
echo "Finding and copying required libraries (excluding libc.so.6)..."
for binary in helm-lambda-layer/python/bin/* helm-lambda-layer/python/libexec/git-core/*; do
    if [ -f "$binary" ] && [ -x "$binary" ]; then
        echo "Analyzing dependencies for $binary..."
        ldd "$binary" 2>/dev/null | \
            grep "=> /" | \
            awk '{print $3}' | \
            grep -v 'libc.so.6' | \
            while read -r lib; do
                if [ -f "$lib" ]; then
                    echo "Copying $lib..."
                    cp -L "$lib" helm-lambda-layer/python/lib/
                fi
            done
    fi
done

echo "Verifying layer contents..."
echo "=== Contents of python/bin ==="
ls -la helm-lambda-layer/python/bin/
echo "=== Contents of python/libexec/git-core ==="
ls -la helm-lambda-layer/python/libexec/git-core/
echo "=== Contents of python/lib ==="
ls -la helm-lambda-layer/python/lib/

# Show component sizes before zipping
echo "=== Component sizes ==="
du -sh helm-lambda-layer/python/bin/*
du -sh helm-lambda-layer/python/libexec/git-core
du -sh helm-lambda-layer/python/lib

echo "Creating zip file..."
# Create the layer zip file with maximum compression
cd helm-lambda-layer
zip -9 -r ../helm-lambda-layer.zip .
cd ..

# Show final zip size
echo "=== Final zip size ==="
du -sh helm-lambda-layer.zip

# Show uncompressed size for verification
echo "=== Uncompressed size ==="
unzip -l helm-lambda-layer.zip | tail -1 | awk '{print $1}'

echo "Verifying zip contents..."
unzip -l helm-lambda-layer.zip | grep git

echo "Layer build complete!"
