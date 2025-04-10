
#!/bin/bash

# Function to handle errors
handle_error() {
    echo "Error: $1"
    exit 1
}

echo "Building the Docker image..."
IMAGE_TAG="berylliumsec/nebula:latest"
docker build -t "$IMAGE_TAG" . || handle_error "Docker build failed"


# echo "Pushing the Docker image to ACR..."
# docker push "$IMAGE_TAG" || handle_error "Docker push failed"

#Remove any dangling images to keep the local environment clean
echo "Cleaning up dangling images..."
docker image prune -f || handle_error "Failed to clean up dangling images"

echo "The Docker image has been successfully built and pushed (overwriting the existing image locally and in ACR) to $IMAGE_TAG"
