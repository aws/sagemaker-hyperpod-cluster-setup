# Dockerfile
FROM --platform=linux/amd64 amazonlinux:2023

# Install required tools
RUN yum install -y zip unzip tar gzip findutils which file util-linux wget

# Set working directory
WORKDIR /layer

# Copy the build script
COPY build-layer.sh .