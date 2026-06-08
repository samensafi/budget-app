#!/bin/bash
# Budget. Double-click this file to start the app.
#
# First launch  : sets up a private Python environment inside userdata/venv
#                 (a few minutes, one time only), then opens Budget in your
#                 web browser.
# Later launches: starts right away.
#
# All your data (budget.db, your backups, and this Python environment) lives
# inside the userdata/ folder and never leaves your computer.
#
# If macOS says the file cannot be opened because it is from an unidentified
# developer: right-click this file, choose Open, then Open again. You only
# have to do that once.

cd "$(dirname "$0")" || exit 1
HERE="$PWD"                          # .../budget-app/app  (the shareable code)
DATA="$(dirname "$HERE")/userdata"   # .../budget-app/userdata  (this person's data)

# Keep the app/ code folder clean: send Python's compiled-bytecode cache
# (__pycache__) into userdata/ instead, so it's never part of a transfer.
export PYTHONPYCACHEPREFIX="$DATA/pycache"

# 0. Stop any copy of this Budget that's already running, so a fresh launch
#    always takes over (frees port 8080) instead of refusing to start.
if pkill -f "$HERE/app.py" >/dev/null 2>&1; then
  sleep 1   # the old copy quits instantly, give it a moment to release the port
fi

# 1. Budget needs Python 3.
if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "Budget needs Python 3, which isn't installed on this Mac yet."
  echo "Install it from  https://www.python.org/downloads/  then run this again."
  echo ""
  read -n 1 -s -r -p "Press any key to close this window."
  exit 1
fi

mkdir -p "$DATA"

VENV="$DATA/venv"
PYBIN="$VENV/bin/python"
STAMP="$VENV/.requirements.sha"

# 2. Create the private environment on first run (kept in userdata/, never shared).
if [ ! -x "$PYBIN" ]; then
  echo "Setting up Budget for the first time. This takes a few minutes..."
  if ! python3 -m venv "$VENV"; then
    echo "Could not create the Python environment."
    read -n 1 -s -r -p "Press any key to close this window."
    exit 1
  fi
  "$PYBIN" -m pip install --upgrade pip >/dev/null 2>&1
fi

# 3. Install the libraries Budget needs, but only when requirements.txt has
#    changed (so normal launches are instant). A small stamp file records which
#    requirements are already installed.
WANT="$(shasum requirements.txt | awk '{print $1}')"
HAVE="$(cat "$STAMP" 2>/dev/null)"
if [ "$WANT" != "$HAVE" ]; then
  echo "Installing the libraries Budget needs (one time)..."
  if "$PYBIN" -m pip install -r requirements.txt; then
    echo "$WANT" > "$STAMP"
  else
    echo ""
    echo "Could not install the required libraries."
    echo "Check your internet connection and run this again."
    read -n 1 -s -r -p "Press any key to close this window."
    exit 1
  fi
fi

# 4. Launch. Your browser opens automatically at http://localhost:8080
echo "Starting Budget... your browser will open at http://localhost:8080"
echo "(Leave this window open while you use Budget. Press Ctrl+C here to quit.)"
exec "$PYBIN" "$HERE/app.py"
