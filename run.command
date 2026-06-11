#!/bin/bash
# Budget. Double-click this file to start the app.
#
# First launch  : sets up a private Python environment inside userdata/venv
#                 (a few minutes, one time only), then opens Budget in your
#                 web browser. The exact Python version Budget needs is
#                 downloaded automatically, so it works the same on every Mac.
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

mkdir -p "$DATA"

VENV="$DATA/venv"
PYBIN="$VENV/bin/python"
STAMP="$VENV/.requirements.sha"

# a damaged environment (an interrupted first setup, a full disk) would otherwise fail
# the exact same way on every launch, forever. if its python can't even start, wipe the
# environment so the normal setup below rebuilds it from scratch. it holds no user data.
if [ -e "$VENV" ] && ! "$PYBIN" -c "" >/dev/null 2>&1; then
  echo "Budget's environment looks damaged. Rebuilding it (a few minutes)..."
  rm -rf "$VENV"
fi

# 1. Find uv, the tool that builds Budget's private environment. It pins the
#    exact Python version Budget needs (see .python-version), downloading it if
#    this Mac doesn't have it, so the app behaves the same everywhere. Install
#    uv once if it isn't here yet.
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
else
  echo "Setting up Budget for the first time. Getting a small helper (uv)..."
  export UV_INSTALL_DIR="$HOME/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
  UV="$HOME/.local/bin/uv"
  if [ ! -x "$UV" ]; then
    echo ""
    echo "Could not set up Budget's helper tool. Check your internet connection and try again."
    # only wait for a key when a person is actually at a terminal
    [ -t 0 ] && read -n 1 -s -r -p "Press any key to close this window."
    exit 1
  fi
fi

# 2. Create the private environment on first run (kept in userdata/, never
#    shared). uv fetches the right Python version automatically.
if [ ! -x "$PYBIN" ]; then
  echo "Setting up Budget for the first time. This takes a few minutes..."
  if ! "$UV" venv "$VENV" --python 3.11 >/dev/null 2>&1; then
    echo "Could not create the Python environment. Check your internet connection and try again."
    [ -t 0 ] && read -n 1 -s -r -p "Press any key to close this window."
    exit 1
  fi
fi

# 3. Install the libraries Budget needs, but only when requirements.txt has
#    changed (so normal launches are instant). A small stamp file records which
#    requirements are already installed.
WANT="$(shasum requirements.txt | awk '{print $1}')"
HAVE="$(cat "$STAMP" 2>/dev/null)"
if [ "$WANT" != "$HAVE" ]; then
  echo "Installing the libraries Budget needs (one time)..."
  if "$UV" pip install --python "$PYBIN" -r requirements.txt; then
    echo "$WANT" > "$STAMP"
  else
    echo ""
    echo "Could not install the required libraries. Check your internet connection and try again."
    [ -t 0 ] && read -n 1 -s -r -p "Press any key to close this window."
    exit 1
  fi
fi

# 4. Launch. Your browser opens automatically at http://localhost:8080
echo "Starting Budget... your browser will open at http://localhost:8080"
echo "(Leave this window open while you use Budget. Press Ctrl+C here to quit.)"
exec "$PYBIN" "$HERE/app.py"
