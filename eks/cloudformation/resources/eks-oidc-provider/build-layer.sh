#!/bin/bash
set -e  # Exit on any error

echo "Starting layer build..."

# Create directory structure for Python runtime
mkdir -p eks-oidc-provider-layer/python/bin
mkdir -p eks-oidc-provider-layer/python/lib

# Set version
EKSCTL_VERSION="0.195.0"

# Download and install eksctl
curl -sLO "https://github.com/eksctl-io/eksctl/releases/download/v${EKSCTL_VERSION}/eksctl_Linux_amd64.tar.gz"
tar -xzf eksctl_Linux_amd64.tar.gz -C /tmp && rm eksctl_Linux_amd64.tar.gz
mv /tmp/eksctl eks-oidc-provider-layer/python/bin/
chmod +x eks-oidc-provider-layer/python/bin/eksctl

# Copy shared libraries
echo "Copying shared libraries..."
echo "Finding and copying required libraries (excluding libc.so.6)..."
for binary in eks-oidc-provider-layer/python/bin/*; do
    if [ -f "$binary" ] && [ -x "$binary" ]; then
        echo "Analyzing dependencies for $binary..."
        ldd "$binary" 2>/dev/null | \
            grep "=> /" | \
            awk '{print $3}' | \
            grep -v 'libc.so.6' | \
            while read -r lib; do
                if [ -f "$lib" ]; then
                    echo "Copying $lib..."
                    cp -L "$lib" eks-oidc-provider-layer/python/lib/
                fi
            done
    fi
done

echo "Verifying layer contents..."
echo "=== Contents of python/bin ==="
ls -la eks-oidc-provider-layer/python/bin/
echo "=== Contents of python/lib ==="
ls -la eks-oidc-provider-layer/python/lib/

# Show component sizes before zipping
echo "=== Component sizes ==="
du -sh eks-oidc-provider-layer/python/bin/*
du -sh eks-oidc-provider-layer/python/lib

echo "Creating zip file..."
# Create the layer zip file with maximum compression
cd eks-oidc-provider-layer
zip -9 -r ../eks-oidc-provider-lambda-layer.zip .
cd ..

# Show final zip size
echo "=== Final zip size ==="
du -sh eks-oidc-provider-lambda-layer.zip

# Show uncompressed size for verification
echo "=== Uncompressed size ==="
unzip -l eks-oidc-provider-lambda-layer.zip | tail -1 | awk '{print $1}'

echo "Layer build complete!"
