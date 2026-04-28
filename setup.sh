#!/usr/bin/env bash
set -euo pipefail

echo "BadgerBot Hyper — Setup"
echo "========================"

# Check for Python 3.12+
if command -v python3.12 &>/dev/null; then
    PYTHON=python3.12
elif command -v python3 &>/dev/null && python3 -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)" 2>/dev/null; then
    PYTHON=python3
else
    echo ""
    echo "ERROR: Python 3.12 or higher is required."
    echo "Install it with:"
    echo "  sudo apt update && sudo apt install -y python3.12 python3.12-venv"
    exit 1
fi

echo "Using: $($PYTHON --version)"

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
else
    echo "Virtual environment already exists, skipping."
fi

# Install dependencies
echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "Dependencies installed."

# Copy .env.example to .env
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "Created .env from .env.example"
else
    echo ".env already exists, skipping."
fi

echo ""
echo "Dependencies installed, please continue the readme steps."