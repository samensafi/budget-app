#!/bin/bash
# Budget launcher: the single source of truth for how Budget starts. build-app.command
# copies this file verbatim into Budget.app (the double-click app on the GitHub Releases
# page), so the launcher and the app can never drift apart.
#
# If Budget's launch contract changes (port 8080, BUDGET_APP_MANAGED, run.command, the
# app.py name, the app/+userdata/ layout; see app/CLAUDE.md), update this file, then
# rebuild and re-upload the download: bash build-app.command, then upload the new
# Budget.zip to a release. Editing the built .app does not update this file or vice versa.
#
# What it does: finds the app folder on its own (no hardcoded path); if Budget
# isn't installed yet it offers to download it from GitHub; runs the server silently
# in managed mode, sets itself up on first run, and shuts everything down the
# moment the app's Safari tab/window is closed.

PORT=8080
# Use the numeric IPv4 loopback, not localhost. On macOS localhost resolves to
# both 127.0.0.1 and IPv6 ::1, and browsers (Safari and Chrome) try ::1 first, but the
# server binds IPv4 only (NiceGUI host defaults to 0.0.0.0). The HTML page still loads
# (the browser falls back to IPv4), but the live-update WebSocket does not fall back
# cleanly, so the app looks frozen, clicks register yet the UI only updates after a
# manual refresh. Opening 127.0.0.1 directly (exactly what run.command/NiceGUI do, and
# why that path works) avoids the IPv6 detour entirely. Used for the opened URL, the
# health check, and the watchdog's tab match below, they must stay in sync.
HOST="127.0.0.1"
CONFIG_FILE="$HOME/.budget-app-path"     # remembers a manually-entered path
REPO_URL="https://github.com/samensafi/budget-app.git"  # where to download Budget from
INSTALL_PARENT="$HOME/budget-app"        # a fresh install goes here ($INSTALL_PARENT/app)

note() { osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with icon caution with title \"Budget\"" >/dev/null 2>&1; }

# Turn a user-typed path into the app/ folder (accepts the budget-app folder or
# its inner app/ folder). Echoes the resolved app/ dir, or returns 1.
resolve_input() {
  local p="$1"
  p="${p#"${p%%[![:space:]]*}"}"     # trim leading whitespace
  p="${p%"${p##*[![:space:]]}"}"     # trim trailing whitespace
  p="${p%\"}"; p="${p#\"}"           # strip surrounding double quotes
  p="${p%/}"                          # strip trailing slash
  case "$p" in "~"*) p="$HOME${p#\~}";; esac   # expand a leading ~
  if [ -f "$p/run.command" ]; then printf '%s\n' "$p"; return 0; fi
  if [ -f "$p/app/run.command" ]; then printf '%s\n' "$p/app"; return 0; fi
  return 1
}

# Find the app/ folder with no hardcoded path. Order:
#   env override, remembered path, this script's own dir, common spots,
#   a search under the home folder and /Applications.
find_app_dir() {
  if [ -n "$BUDGET_APP_DIR" ] && [ -f "$BUDGET_APP_DIR/run.command" ]; then
    printf '%s\n' "$BUDGET_APP_DIR"; return 0
  fi
  if [ -f "$CONFIG_FILE" ]; then
    local saved; saved="$(cat "$CONFIG_FILE" 2>/dev/null)"
    [ -n "$saved" ] && [ -f "$saved/run.command" ] && { printf '%s\n' "$saved"; return 0; }
  fi
  local self_dir
  self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  [ -n "$self_dir" ] && [ -f "$self_dir/run.command" ] && { printf '%s\n' "$self_dir"; return 0; }
  local g
  for g in \
    "$HOME/budget-app/app" \
    "$HOME/Desktop/budget-app/app" \
    "$HOME/Documents/budget-app/app" \
    "$HOME/Downloads/budget-app/app" \
    "$HOME/Applications/budget-app/app" \
    "/Applications/budget-app/app"; do
    [ -f "$g/run.command" ] && { printf '%s\n' "$g"; return 0; }
  done
  local hit
  hit="$(find "$HOME" "/Applications" -type f -name run.command -path '*/budget-app/app/run.command' 2>/dev/null | head -n 1)"
  [ -n "$hit" ] && { printf '%s\n' "$(cd "$(dirname "$hit")" && pwd)"; return 0; }
  return 1
}

# Last resort: ask the user to type/paste the path, then remember it for next time.
prompt_for_path() {
  local input chosen
  while true; do
    input="$(osascript \
      -e 'try' \
      -e 'set r to display dialog "Budget could not be found automatically.  Type or paste the full path to your budget-app folder (or the app folder inside it):" default answer "" with title "Budget" buttons {"Cancel", "OK"} default button "OK"' \
      -e 'return text returned of r' \
      -e 'on error' \
      -e 'return "##CANCEL##"' \
      -e 'end try' 2>/dev/null)"
    [ "$input" = "##CANCEL##" ] && return 1
    [ -z "$input" ] && continue
    if chosen="$(resolve_input "$input")"; then
      printf '%s' "$chosen" > "$CONFIG_FILE" 2>/dev/null
      printf '%s\n' "$chosen"; return 0
    fi
    note "That folder does not contain Budget (no run.command was found there). Please try again, or move the budget-app folder somewhere standard like the Desktop."
  done
}

# Not installed anywhere? Offer to download it from GitHub into ~/budget-app/app.
# Needs Git (macOS installs it on first use). Code only, the data folder is made
# later on first run. Echoes the new app/ dir, or returns 1.
offer_install() {
  local ans
  ans="$(osascript \
    -e 'try' \
    -e 'set r to display dialog "Budget is not installed on this Mac yet. Download and install it now? It goes in your home folder and takes a few minutes the first time." with title "Budget" buttons {"Cancel", "Install"} default button "Install"' \
    -e 'return button returned of r' \
    -e 'on error' \
    -e 'return "Cancel"' \
    -e 'end try' 2>/dev/null)"
  [ "$ans" != "Install" ] && return 1

  if ! command -v git >/dev/null 2>&1; then
    xcode-select --install >/dev/null 2>&1
    note "Budget needs Git to install and keep itself updated. A macOS Command Line Tools window should appear, click Install, wait for it to finish, then open Budget again."
    return 1
  fi

  osascript -e 'display notification "Downloading Budget. This takes a few minutes and it will open by itself when ready." with title "Budget"' >/dev/null 2>&1
  mkdir -p "$INSTALL_PARENT"
  if git clone "$REPO_URL" "$INSTALL_PARENT/app" >/dev/null 2>&1 && [ -f "$INSTALL_PARENT/app/run.command" ]; then
    printf '%s' "$INSTALL_PARENT/app" > "$CONFIG_FILE" 2>/dev/null   # remember it
    printf '%s\n' "$INSTALL_PARENT/app"; return 0
  fi
  note "Budget could not be downloaded. Check your internet connection and open Budget again."
  return 1
}

APP_DIR="$(find_app_dir)"
[ -z "$APP_DIR" ] && APP_DIR="$(offer_install)"    # not installed yet? download it
[ -z "$APP_DIR" ] && APP_DIR="$(prompt_for_path)"  # or point at an existing copy
if [ -z "$APP_DIR" ]; then
  note "Budget needs the location of its folder to start. Open Budget again when you can point it to the budget-app folder."
  exit 1
fi

RUN="$APP_DIR/run.command"
DATA_DIR="$(dirname "$APP_DIR")/userdata"
VENV_PY="$DATA_DIR/venv/bin/python"
URL="http://$HOST:$PORT/?app=$(date +%s)"   # ?app=... defeats Safari's cache

# Liveness check. Must hit /healthz, not the page route. Hitting / re-runs the app's page builder
# on every poll, which reassigns the app's shared on-screen state to this throwaway request
# (it never opens a real browser connection) and orphans the actual Budget tab, the app
# then looks frozen (clicks change data but the screen only updates after a manual refresh).
# That was the long-standing frozen-until-I-refresh bug, seen only via this launcher (the
# plain run.command has no watchdog). /healthz is a tiny plain route that does not run the
# page, so polling it can never freeze the app. See app.py (_healthz) and CLAUDE.md item 8.
server_up() { curl -s -o /dev/null --max-time 2 "http://$HOST:$PORT/healthz"; }

# returns y if any Safari tab still points at the app, else n (or empty if Safari quit).
app_open() {
  osascript 2>/dev/null <<EOF
tell application "Safari"
  set found to "n"
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (URL of t) contains "$HOST:$PORT" then set found to "y"
      end try
    end repeat
  end repeat
  return found
end tell
EOF
}

kill_app() {
  pkill -f "$APP_DIR/app.py" >/dev/null 2>&1
  local pids
  pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null)
  if [ -n "$pids" ]; then
    kill $pids >/dev/null 2>&1
    sleep 1
    pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null)
    [ -n "$pids" ] && kill -9 $pids >/dev/null 2>&1
  fi
}

trap 'kill_app; exit 0' INT TERM

# 0. Free port 8080 so Budget can always start, killing whatever holds it. If the holder
#    is something OTHER than a leftover Budget, let the user know we're clearing it first.
port_pids=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null)
for pid in $port_pids; do
  cmd=$(ps -p "$pid" -o command= 2>/dev/null)
  [ -z "$cmd" ] && continue                  # process already gone
  case "$cmd" in *app.py*) continue ;; esac  # our own leftover, no warning needed
  osascript -e 'display notification "Another app was using port 8080. Budget freed it so it can start." with title "Budget"' >/dev/null 2>&1
  break
done
kill_app

# Budget no longer updates itself on launch. The app checks GitHub on its own and shows
# an in-app banner with an Update now button when a new version is available, so updating
# is the user's choice (see check_for_update / apply_update in app.py).

# 1. First run? (no venv yet), allow a long, silent one-time setup and
#    tell the user it's working so the wait isn't a black hole.
if [ -x "$VENV_PY" ]; then
  MAX=240          # ~120s: already set up, just starting.
else
  MAX=1200         # ~600s: first launch builds the private environment.
  osascript -e 'display notification "Setting up Budget for the first time. This takes a few minutes and it will open by itself when ready." with title "Budget"' >/dev/null 2>&1
fi

# 2. Start the server silently in managed mode (no Terminal, no auto-open).
export BUDGET_APP_MANAGED=1
bash "$RUN" >/dev/null 2>&1 &
RUN_PID=$!

# 3. Wait for the server. Succeed when it answers, fail fast if the
#    background process dies first (setup error), give up after MAX.
tries=0
until server_up; do
  if ! kill -0 "$RUN_PID" 2>/dev/null; then
    kill_app
    note "Budget couldn't finish starting. If this was the first launch, check your internet connection, then open Budget again."
    exit 1
  fi
  tries=$((tries + 1))
  if [ "$tries" -ge "$MAX" ]; then
    kill_app
    note "Budget is taking too long to start. Please open it again."
    exit 1
  fi
  sleep 0.5
done

# 4. Open Budget in a new Safari window and bring Safari to the front.
winID=$(osascript <<EOF 2>/dev/null
tell application "Safari"
  activate
  make new document with properties {URL:"$URL"}
  delay 0.3
  return id of front window
end tell
EOF
)

if [ -z "$winID" ]; then
  kill_app
  note "Budget needs permission to control Safari. Open System Settings > Privacy & Security > Automation, allow this app to control Safari, then launch Budget again."
  exit 1
fi

# 5. Watchdog, when the app's tab/window is gone (or the server dies), stop everything.
while true; do
  sleep 2
  if [ "$(app_open)" != "y" ]; then    # tab/window closed, or navigated away.
    break
  fi
  if ! server_up; then                 # server crashed on its own.
    sleep 2
    if ! server_up; then break; fi
  fi
done

# 6. Final cleanup, then let the launcher app quit.
kill_app
exit 0
