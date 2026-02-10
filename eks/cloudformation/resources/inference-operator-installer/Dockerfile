# Dockerfile
FROM --platform=linux/amd64 amazonlinux:2023

# Install required tools
RUN yum install -y zip python3 python3-pip

# Set working directory
WORKDIR /layer

# Copy the build script and lambda function directory
COPY build-layer.sh .
COPY lambda_function/ ./lambda_function/
