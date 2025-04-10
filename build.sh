
#!/bin/bash

# Function to handle errors
handle_error() {
    echo "Error: $1"
    exit 1
}

echo "Building the Docker image..."
IMAGE_TAG="berylliumsec/nebula:latest"
docker build -t "$IMAGE_TAG" . || handle_error "Docker build failed"
