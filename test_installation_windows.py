#!/usr/bin/env python3
"""
Nebula Installation Verification Script for Windows

This script checks if Nebula is installed correctly and verifies the availability of required components.
"""

import os
import sys
import importlib
import subprocess
from pathlib import Path

def check_python_version():
    print(f"Checking Python version...")
    major, minor = sys.version_info[:2]
    if major == 3 and minor >= 11:
        print(f"✓ Python version {major}.{minor} is compatible.")
        return True
    else:
        print(f"✗ Python version {major}.{minor} is not compatible. Nebula requires Python 3.11+")
        return False

def check_required_modules():
    print("Checking required modules...")
    required_modules = [
        "PyQt6", "torch", "transformers", "langchain", "ollama", "regex", 
        "Cython", "IPython", "fastapi", "uvicorn", "pydantic"
    ]
    
    missing_modules = []
    for module in required_modules:
        try:
            importlib.import_module(module)
            print(f"✓ {module} is installed.")
        except ImportError:
            print(f"✗ {module} is not installed.")
            missing_modules.append(module)
    
    if missing_modules:
        print(f"\nMissing modules: {', '.join(missing_modules)}")
        print("Install them with: pip install " + " ".join(missing_modules))
        return False
    return True

def check_nebula_directories():
    print("Checking Nebula directories...")
    # Windows path
    nebula_dir = Path(os.environ["USERPROFILE"]) / ".local" / "share" / "nebula"
    
    directories = [
        nebula_dir,
        nebula_dir / "logs",
        nebula_dir / "cache",
        nebula_dir / "data"
    ]
    
    all_exist = True
    for directory in directories:
        if directory.exists():
            print(f"✓ {directory} exists.")
        else:
            print(f"✗ {directory} does not exist.")
            all_exist = False
    
    if not all_exist:
        print("\nSome directories are missing. Run the installation script to create them.")
        return False
    return True

def check_nebula_command():
    print("Checking if Nebula command is available...")
    try:
        # For Windows, check if nebula.exe is in the Python Scripts directory
        python_path = sys.executable
        scripts_dir = Path(python_path).parent / "Scripts"
        nebula_path = scripts_dir / "nebula.exe"
        
        if nebula_path.exists():
            print(f"✓ Nebula command found at: {nebula_path}")
            return True
        else:
            # Try with where command
            try:
                result = subprocess.run(["where", "nebula"], capture_output=True, text=True, check=False)
                if result.returncode == 0:
                    print(f"✓ Nebula command found at: {result.stdout.strip()}")
                    return True
                else:
                    print("✗ Nebula command not found in PATH.")
                    return False
            except Exception:
                print("✗ Nebula command not found in PATH or Scripts directory.")
                return False
    except Exception as e:
        print(f"✗ Error checking Nebula command: {e}")
        return False

def main():
    print("==== Nebula Installation Verification for Windows ====\n")
    
    python_ok = check_python_version()
    modules_ok = check_required_modules()
    directories_ok = check_nebula_directories()
    command_ok = check_nebula_command()
    
    print("\n==== Verification Summary ====")
    print(f"Python Version: {'✓' if python_ok else '✗'}")
    print(f"Required Modules: {'✓' if modules_ok else '✗'}")
    print(f"Nebula Directories: {'✓' if directories_ok else '✗'}")
    print(f"Nebula Command: {'✓' if command_ok else '✗'}")
    
    if python_ok and modules_ok and directories_ok and command_ok:
        print("\n✅ Nebula is installed correctly and ready to use!")
        print("\nRun 'nebula' to start the application.")
    else:
        print("\n❌ Some components are missing. Please fix the issues above and try again.")

if __name__ == "__main__":
    main()