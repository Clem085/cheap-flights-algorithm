#!/usr/bin/env bash
set -euo pipefail

if ! command -v python >/dev/null 2>&1; then
    echo "Python is not installed. On Arch: sudo pacman -S python" >&2
    exit 1
fi

if ! command -v chromium >/dev/null 2>&1 &&
   ! command -v google-chrome-stable >/dev/null 2>&1 &&
   ! command -v google-chrome >/dev/null 2>&1 &&
   ! command -v brave >/dev/null 2>&1; then
    echo "No Chromium-family browser found." >&2
    echo "On Arch: sudo pacman -S chromium" >&2
    exit 1
fi

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Setup complete."
echo "Activate later with: source .venv/bin/activate"
echo "Then run: python smoke_test.py"
