# Nebula Installation Script for Windows

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "      Nebula Installation Script for Windows" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

# Check Python version
try {
    $pythonVersion = (python --version 2>&1) -replace "Python "
    $versionParts = $pythonVersion.Split(".")
    $major = [int]$versionParts[0]
    $minor = [int]$versionParts[1]
    
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
        Write-Host "Error: Python 3.11 or higher is required. You have $pythonVersion" -ForegroundColor Red
        exit 1
    }
    
    Write-Host "✓ Python version check passed: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "Error: Python not found or version check failed." -ForegroundColor Red
    Write-Host "Please install Python 3.11 or higher and ensure it's in your PATH." -ForegroundColor Red
    exit 1
}

# Create necessary directories
Write-Host "Creating necessary directories..." -ForegroundColor Yellow
$nebulaDir = "$env:USERPROFILE\.local\share\nebula"
New-Item -ItemType Directory -Force -Path "$nebulaDir\logs" | Out-Null
New-Item -ItemType Directory -Force -Path "$nebulaDir\cache" | Out-Null
New-Item -ItemType Directory -Force -Path "$nebulaDir\data" | Out-Null

Write-Host "✓ Directories created" -ForegroundColor Green

# Install dependencies using pip
Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Yellow
& python -m pip install -r requirements.txt

# Install Nebula
Write-Host "Installing Nebula..." -ForegroundColor Yellow
if (Test-Path "setup.py") {
    & python -m pip install -e .
} else {
    & python -m pip install -e .
}

Write-Host "✓ Nebula installed successfully" -ForegroundColor Green

# Check if Ollama is installed
$ollamaPath = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollamaPath) {
    Write-Host "✓ Ollama is installed" -ForegroundColor Green
    $downloadModels = Read-Host "Would you like to download the required models for Ollama? (y/n)"
    
    if ($downloadModels -eq "y" -or $downloadModels -eq "Y") {
        Write-Host "Downloading Ollama models (this may take some time)..." -ForegroundColor Yellow
        & ollama pull mistral
        & ollama pull deepseek-r1
        & ollama pull llama3.1
        Write-Host "✓ Models downloaded successfully" -ForegroundColor Green
    }
} else {
    Write-Host "! Ollama is not installed. If you want to use Ollama models, please install it from https://ollama.com/" -ForegroundColor Yellow
}

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "Installation Complete!" -ForegroundColor Green
Write-Host ""
Write-Host "To run Nebula, type:" -ForegroundColor White
Write-Host "    nebula" -ForegroundColor White
Write-Host ""
Write-Host "For Hugging Face models, you'll need to set your token:" -ForegroundColor White
Write-Host "    $env:HF_TOKEN='your_token_here'" -ForegroundColor White
Write-Host ""
Write-Host "If you encounter any issues, check the logs at:" -ForegroundColor White
Write-Host "    $nebulaDir\logs" -ForegroundColor White
Write-Host "======================================================" -ForegroundColor Cyan