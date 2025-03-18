#!/bin/bash

# Nebula Installation Script for Linux
set -e

echo "======================================================"
echo "      Nebula Installation Script for Linux"
echo "======================================================"

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo "Error: Python 3.11 or higher is required. You have $PYTHON_VERSION"
    exit 1
fi

echo "✓ Python version check passed: $PYTHON_VERSION"

# Create necessary directories
echo "Creating necessary directories..."
NEBULA_DIR="$HOME/.local/share/nebula"
mkdir -p "$NEBULA_DIR/logs"
mkdir -p "$NEBULA_DIR/cache"
mkdir -p "$NEBULA_DIR/data"

echo "✓ Directories created"

# Install dependencies using pip
echo "Installing dependencies from requirements.txt..."
pip install -r requirements.txt

# Install Nebula
echo "Installing Nebula..."
if [ -f "setup.py" ]; then
    pip install -e .
else
    pip install -e .
fi

echo "✓ Nebula installed successfully"

# Check if Ollama is installed
if command -v ollama >/dev/null 2>&1; then
    echo "✓ Ollama is installed"
    echo "Would you like to download the required models for Ollama? (y/n)"
    read -r download_models
    
    if [ "$download_models" = "y" ] || [ "$download_models" = "Y" ]; then
        echo "Downloading Ollama models (this may take some time)..."
        ollama pull mistral
        ollama pull deepseek-r1
        ollama pull llama3.1
        echo "✓ Models downloaded successfully"
    fi
else
    echo "! Ollama is not installed. If you want to use Ollama models, please install it from https://ollama.com/"
fi

# Set permissions
echo "Setting permissions..."
chmod -R 755 "$NEBULA_DIR"

echo "======================================================"
echo "Installation Complete!"
echo ""
echo "To run Nebula, type:"
echo "    nebula"
echo ""
echo "For Hugging Face models, you'll need to set your token:"
echo "    export HF_TOKEN=your_token_here"
echo ""
echo "If you encounter any issues, check the logs at:"
echo "    $NEBULA_DIR/logs"
echo "======================================================"